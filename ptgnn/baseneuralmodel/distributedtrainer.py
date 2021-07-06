from typing_extensions import Final

import json
import logging
import math
import os
import time
import torch
import torch.distributed as dist
import torch.multiprocessing as mp
from functools import partial
from torch.distributed.optim import ZeroRedundancyOptimizer
from torch.nn.parallel import DistributedDataParallel as DDP
from typing import Iterable, TypeVar

from ptgnn.baseneuralmodel import AbstractScheduler, ModelTrainer
from ptgnn.baseneuralmodel.abstractneuralmodel import AbstractNeuralModel
from ptgnn.baseneuralmodel.modulewithmetrics import ModuleWithMetrics
from ptgnn.baseneuralmodel.utils.amlutils import configure_logging
from ptgnn.baseneuralmodel.utils.data import ShardedLazyDataIterable

TRawDatapoint = TypeVar("TRawDatapoint")
TTensorizedDatapoint = TypeVar("TTensorizedDatapoint")
TNeuralModule = TypeVar("TNeuralModule", bound=ModuleWithMetrics)
ModelType = AbstractNeuralModel[TRawDatapoint, TTensorizedDatapoint, TNeuralModule]

__all__ = ["DistributedModelTrainer"]


class DistributedModelTrainer(ModelTrainer[TRawDatapoint, TTensorizedDatapoint, TNeuralModule]):
    """
    A distributed trainer for `AbstractComponent`s. Used mainly for supervised learning.

    Create a `DistributedModelTrainer` by passing a `AbstractNeuralModel` in the constructor.
    Invoke `train()` to initiate the training loop. The root `TNeuralModule` should return a scalar loss.
    """

    LOGGER: Final = logging.getLogger(__name__)

    def _run_training(
        self,
        distibuted_module: DDP,
        training_tensors,
        epoch,
        device,
        optimizer,
        parallelize,
        scheduler,
        shuffle_input: bool = True,
    ):
        sum_epoch_loss, num_minibatches, num_samples = 0.0, 0, 0
        start_time = time.time()
        distibuted_module.train()

        scaler = torch.cuda.amp.GradScaler(enabled=self._enable_amp)
        try:
            with distibuted_module.join():
                for step_idx, (mb_data, raw_samples) in enumerate(
                    self.model.minibatch_iterator(
                        training_tensors(),
                        device=device,
                        max_minibatch_size=self._minibatch_size,
                        yield_partial_minibatches=False,
                        shuffle_input=shuffle_input,
                        parallelize=parallelize,
                    )
                ):
                    self.LOGGER.info("Step %i", step_idx)
                    optimizer.zero_grad()
                    with torch.cuda.amp.autocast(enabled=self._enable_amp):
                        mb_loss = distibuted_module(**mb_data)
                        if torch.isnan(mb_loss):
                            raise Exception("Loss has a NaN value.")

                        scaler.scale(mb_loss).backward()

                        if self._clip_gradient_norm is not None:
                            scaler.unscale_(optimizer)
                            torch.nn.utils.clip_grad_norm_(
                                distibuted_module.parameters(recurse=True), self._clip_gradient_norm
                            )

                        scaler.step(optimizer)
                        scaler.update()
                        if scheduler is not None:
                            scheduler.step(epoch_idx=epoch, epoch_step=step_idx)

                    num_minibatches += 1
                    num_samples += len(raw_samples)
                    sum_epoch_loss += float(mb_loss)
                    self.LOGGER.info("End of step: %s", step_idx)
            self.LOGGER.info("Exited Join")

        except Exception as e:
            self.LOGGER.exception("Something went wrong: %s", str(e))
            raise e

        elapsed_time = time.time() - start_time
        self.LOGGER.info(
            "Training complete in %.1fsec [%.2f samples/sec]",
            elapsed_time,
            num_samples / elapsed_time,
        )
        assert (
            num_minibatches > 0
        ), "No training minibatches were created. The minibatch size may be too large or the training dataset size too small."
        self.LOGGER.info("Epoch %i: Train Loss %.2f", epoch + 1, sum_epoch_loss / num_minibatches)
        train_metrics = distibuted_module.module.report_metrics()

        for epoch_hook in self._train_epoch_end_hooks:
            epoch_hook(self.model, distibuted_module.module, epoch, train_metrics)

        if len(train_metrics) > 0:
            self.LOGGER.info("Training Metrics: %s", json.dumps(train_metrics, indent=2))

    def _run_validation(
        self,
        distributed_neural_module: DDP,
        validation_tensors,
        epoch,
        best_target_metric,
        device,
        parallelize,
    ):
        distributed_neural_module.eval()
        sum_epoch_loss, num_minibatches, num_samples = 0.0, 0, 0
        start_time = time.time()
        try:
            with torch.no_grad():
                for mb_data, raw_samples in self.model.minibatch_iterator(
                    validation_tensors(),
                    device=device,
                    max_minibatch_size=self._minibatch_size,
                    yield_partial_minibatches=True,
                    shuffle_input=False,
                    parallelize=parallelize,
                ):
                    with torch.cuda.amp.autocast(enabled=self._enable_amp):
                        mb_loss = distributed_neural_module(**mb_data)
                    num_minibatches += 1
                    num_samples += len(raw_samples)
                    sum_epoch_loss += mb_loss

                elapsed_time = time.time() - start_time
                assert num_samples > 0, "No validation data was found."

                # Sync validation losses
                validation_loss = sum_epoch_loss / num_minibatches
                dist.all_reduce(validation_loss)
                validation_loss = validation_loss.item() / dist.get_world_size()
        except RuntimeError as re:
            self.LOGGER.exception("Something went wrong: %s", str(re))

        self.LOGGER.info(
            "Validation complete in %.1fsec [%.2f samples/sec]",
            elapsed_time,
            (num_samples / elapsed_time),
        )
        self.LOGGER.info("Epoch %i: Valid Loss %.2f", epoch + 1, validation_loss)

        validation_metrics = distributed_neural_module.module.report_metrics()
        for epoch_hook in self._validation_epoch_end_hooks:
            epoch_hook(self.model, distributed_neural_module.module, epoch, validation_metrics)
        if len(validation_metrics) > 0:
            self.LOGGER.info("Validation Metrics: %s", json.dumps(validation_metrics, indent=2))

        if self._target_metric is not None:
            target_metric = validation_metrics[self._target_metric]
        else:
            target_metric = validation_loss

        if self._target_metric_higher_is_better:
            target_metric_improved = target_metric > best_target_metric
        else:
            target_metric_improved = target_metric < best_target_metric

        return target_metric, target_metric_improved

    def train(
        self,
        training_data: Iterable[TRawDatapoint],
        validation_data: Iterable[TRawDatapoint],
        *,
        show_progress_bar: bool = True,
        validate_on_start: bool = True,
        patience: int = 5,
        initialize_metadata: bool = True,
        parallelize: bool = True,
        use_multiprocessing: bool = True,
        exponential_running_average_factor: float = 0.97,
        device=None,
        store_tensorized_data_in_memory: bool = False,
        shuffle_training_data: bool = True,
    ) -> None:
        raise Exception("Use `distributed_train` instead.")

    def distributed_train(
        self,
        training_data: ShardedLazyDataIterable[TRawDatapoint],
        validation_data: ShardedLazyDataIterable[TRawDatapoint],
        *,
        validate_on_start: bool = True,
        patience: int = 5,
        initialize_metadata: bool = True,
        parallelize: bool = True,
        shuffle_training_data: bool = True,
    ) -> None:
        """
        The training-validation loop for `AbstractNeuralModel`s.

        :param training_data: An iterable that each iteration yields the full training data. Note
            that the data iterator should be aware of the rank of the node. Use `dist.get_rank()` to achieve that.
        :param validation_data: An iterable that each iteration yields the full validation data. Note
            that the data iterator should be aware of the rank of the node. Use `dist.get_rank()` to achieve that.
        :param validate_on_start: Whether to run a validation loop on start
        :param patience: The number of iterations before early stopping kicks in.
        :param initialize_metadata: If true, initialize the metadata from the training_data. Otherwise,
            assume that the model that is being trained has its metadata already initialized.
        :param parallelize: Bool indicating whether to run in parallel
        :param use_multiprocessing: Whether to use multiprocessing
        :param exponential_running_average_factor: The factor of the running average of the training loss
            displayed in the progress bar.
        :param shuffle_training_data: shuffle the incoming data from `training_data`.
        """
        assert torch.distributed.is_available()

        if initialize_metadata:
            training_data.set_rank(0, 1)
            self.load_metadata_and_create_network(training_data, parallelize, True)

        self.LOGGER.info(
            "Model has %s trainable parameters.",
            sum(
                param.numel()
                for param in self.neural_module.parameters(recurse=True)
                if param.requires_grad
            ),
        )

        ### Distributed code starts here
        # First spawn processes here
        world_size = 2  # TODO: External setup
        mp.spawn(
            self._parallel_training_process,
            args=(
                world_size,
                training_data,
                validation_data,
                parallelize,
                patience,
                shuffle_training_data,
                validate_on_start,
            ),
            nprocs=world_size,
            join=True,
        )
        self._restore_checkpoint()  # TODO: This won't work

    def _parallel_training_process(
        self,
        rank,
        world_size,
        training_data,
        validation_data,
        parallelize,
        patience,
        shuffle_training_data,
        validate_on_start: bool,
    ):

        os.environ["MASTER_ADDR"] = "localhost"
        os.environ["MASTER_PORT"] = "12355"
        dist.init_process_group(backend="nccl", rank=rank, world_size=world_size)

        configure_logging(None, rank=rank)  # TODO: Pass AML context, if any

        self.LOGGER.info(
            f"[{os.getpid()}]: world_size = {dist.get_world_size()}, "
            + f"rank = {dist.get_rank()}, backend={dist.get_backend()}"
        )
        device = dist.get_rank()

        self.neural_module.to(device)
        distributed_neural_module = DDP(self.neural_module, device_ids=[device])

        training_data.set_rank(dist.get_rank(), dist.get_world_size())
        training_tensors = partial(
            self.model.tensorize_dataset,
            training_data,
            parallelize=parallelize,
            use_multiprocessing=True,
        )
        validation_data.set_rank(dist.get_rank(), dist.get_world_size())
        validation_tensors = partial(
            self.model.tensorize_dataset,
            validation_data,
            parallelize=parallelize,
            use_multiprocessing=True,
        )

        # TODO: Configurable
        optimizer = optimizer = ZeroRedundancyOptimizer(
            distributed_neural_module.parameters(), optimizer_class=torch.optim.Adam, lr=0.001
        )
        # optimizer = self._create_optimizer(distributed_neural_module.parameters())

        scheduler = None if self._create_scheduler is None else self._create_scheduler(optimizer)

        for hook in self._training_start_hooks:
            hook(self.model, distributed_neural_module.module, optimizer)

        if self._target_metric_higher_is_better and self._target_metric is not None:
            best_target_metric = -math.inf
        else:
            best_target_metric = math.inf
        if validate_on_start:
            target_metric, improved = self._run_validation(
                distributed_neural_module,
                validation_tensors,
                0,
                best_target_metric,
                device,
                parallelize,
            )
            assert improved
            self.LOGGER.info(f"Initial {self._target_metric or 'Loss'}: {target_metric}")
            best_target_metric = target_metric

        num_epochs_not_improved: int = 0

        for epoch in range(self._max_num_epochs):
            try:
                self._run_training(
                    distributed_neural_module,
                    training_tensors,
                    epoch,
                    device,
                    optimizer,
                    parallelize,
                    scheduler,
                    shuffle_training_data,
                )
            except Exception as e:
                self.LOGGER.exception("Error during training", exc_info=e)
                raise e

            target_metric, target_metric_improved = self._run_validation(
                distributed_neural_module,
                validation_tensors,
                epoch,
                best_target_metric,
                device,
                parallelize,
            )
            if target_metric_improved:
                self.LOGGER.info(
                    f"Best performance so far "
                    f"({self._target_metric or 'Loss'}: {target_metric:.3f} from {best_target_metric:.3f}). "
                    "Saving model checkpoint."
                )
                num_epochs_not_improved = 0
                if dist.get_rank() == 0:
                    self._save_checkpoint()
                best_target_metric = target_metric
            else:
                num_epochs_not_improved += 1
                if num_epochs_not_improved > patience:
                    self.LOGGER.warning(
                        f"The target metric has not improved for {num_epochs_not_improved} epochs . Stopping."
                    )
                    break
        # Restore the best model params that were found.
        dist.destroy_process_group()