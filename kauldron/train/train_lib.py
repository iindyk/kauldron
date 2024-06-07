# Copyright 2024 The kauldron Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Training loop library."""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
import itertools
from typing import Optional

from etils import epath
import jax
import jax.numpy as jnp
from kauldron.evals import eval_impl
from kauldron.inspect import profile_utils
from kauldron.train import checkpoint_state
from kauldron.train import config_lib
from kauldron.train import setup_utils
from kauldron.train import timer as timer_module
from kauldron.train import train_step
from kauldron.train.status_utils import status  # pylint: disable=g-importing-member
from kauldron.utils import utils
from kauldron.utils.sharding_utils import sharding as sharding_lib  # pylint: disable=g-importing-member

# Jax config options
# Required for the `jax.Array` parallelization
jax.config.update("jax_threefry_partitionable", True)


def train_impl(
    trainer: config_lib.Trainer,
) -> tuple[train_step.TrainState, Optional[train_step.Auxiliaries]]:
  """Implements of `Trainer.train`."""
  setup = trainer.setup
  setup.log("Configuring ...")
  setup.run(trainer)

  setup.log("Initializing ...")
  trainstep = trainer.trainstep
  ckpt = trainer.checkpointer
  writer = trainer.writer
  latest_step = ckpt.latest_step
  initial_step = 0 if latest_step is None else latest_step

  state = trainstep.init(
      elem_spec=trainer.train_ds.element_spec,
      skip_transforms=latest_step is not None,
  )
  timer = timer_module.PerformanceTimer(
      initial_step_num=initial_step,
      initial_training_time_hours=0.0,
      per_device_batch_size=trainer.train_ds.batch_size / jax.device_count(),
      global_batch_size=trainer.train_ds.batch_size,
  )
  ds_iter = iter(trainer.train_ds)

  # Initialize CheckpointManager and attempt to restore state.
  (state, timer, ds_iter) = ckpt.restore(
      checkpoint_state.CheckpointState(state, timer, ds_iter),
      noop_if_missing=True,
  )

  if initial_step == 0:
    writer.write_config(trainer.raw_cfg)
    writer.write_param_overview(initial_step, state.params)
    writer.write_element_spec(initial_step, ds_iter.element_spec)
    writer.write_context_structure(initial_step, trainer)

  aux = None

  setup.log(f"Starting training loop at step {initial_step}")
  with _transfer_guard():
    # NOTE: DO *NOT* CHANGE THE ORDER OF OPERATIONS IN THE TRAINING LOOP!
    for i in _enum_steps_with_hooks(
        initial_step=initial_step,
        num_train_steps=trainer.num_train_steps,
        stop_after_steps=trainer.stop_after_steps,
        profiler=trainer.profiler,
        tqdm_info=trainer.setup.tqdm_info,
    ):
      with timer.exclude_from_step_stats():
        if ckpt.should_save(i):
          # Take the time after executing the last training step so
          # that the times logged and stored with the checkpoint match.
          # TODO(epot): Should check `i` and `state.step` match
          ckpt.save(
              checkpoint_state.CheckpointState(state, timer, ds_iter),
              step=i,
          )

        for evaluator in trainer.evals.values():
          evaluator.maybe_eval(step=i, state=state)

      log_summaries = i % trainer.log_summaries_every == 0
      log_metrics = i % trainer.log_metrics_every == 0
      log_any = log_metrics or log_summaries

      batch = next(ds_iter)  # Only mutate `ds_iter` after `ckpt.save`
      batch = sharding_lib.device_put(batch, trainer.sharding.ds)
      state, aux = trainstep.step(
          state,
          batch,
          return_losses=log_any,
          return_metrics=log_metrics,
          return_summaries=log_summaries,
          checkify_error_categories=trainer.checkify_error_categories,
      )
      timer.finish_step()

      # TODO(epot): Should be a `@checkify` decorator in `trainstep.step`.
      if trainer.checkify_error_categories:
        jax.device_get(aux.error).throw()

      if log_any and status.is_lead_host:
        # NOTE: ensure that evaluation metrics are computed from the OLD model
        # state *before* backprop gradients are applied.
        writer.write_step_metrics(
            step=i,
            aux=aux,
            schedules=trainer.schedules,
            model_with_aux=trainstep.model_with_aux,
            timer=timer,
            log_summaries=log_summaries,
        )

  # Notify the eval job training is complete
  if trainer.workdir.exists():  # `TrainEvaluator` do not have a workdir
    epath.Path(trainer.workdir).joinpath(
        eval_impl.TRAIN_COMPLETE_FILENAME
    ).touch()

  _sync()
  # TODO(b/321010908): Should sync the checkpoints
  # ckpt.wait_until_finished()
  # Returning the final state is convenient for interactive training in colab
  return state, aux


def _enum_steps_with_hooks(
    *,
    initial_step: int,
    num_train_steps: Optional[int],
    stop_after_steps: Optional[int],
    profiler: profile_utils.Profiler,
    tqdm_info: setup_utils.TqdmInfo,
) -> Iterator[int]:
  """Enumerate over the train dataset.

  This function:

  * Compute the total number of steps
  * Add hooks for reporting and profiling the train step
  * Add tdqm bar

  Args:
    initial_step: Initial step (e.g. if restoring the checkpoint)
    num_train_steps: Same as `trainer.num_train_steps`
    stop_after_steps: Same as `trainer.stop_after_steps`
    profiler: Same as `trainer.profiler`
    tqdm_info: Additional display parameters for the TQDM bar.

  Yields:
    step: Step number
    batch: Example batch
  """
  # TODO(epot): Currently, setting `num_train_steps=None` will fail. Instead
  # should use `len(ds)` or check `num_epoch is not None`
  if num_train_steps is None:
    raise ValueError(
        "`trainer.num_train_steps is None`. Please provide a value."
    )

  total_steps = num_train_steps + 1
  if stop_after_steps is not None:
    total_steps = min(total_steps, stop_after_steps)

  hooks = []
  if status.is_lead_host:
    hooks.append(profiler)

  for i, _ in utils.enum_iter(
      itertools.repeat(None),
      init_step=initial_step,
      total_steps=total_steps,
      desc=tqdm_info.desc,
      log_xm=tqdm_info.log_xm,
  ):
    yield i
    for h in hooks:
      h(i)


@contextlib.contextmanager
def _transfer_guard() -> Iterator[None]:
  """Prevent implicit device transfer inside the train loop.

  Doc at: https://jax.readthedocs.io/en/latest/transfer_guard.html
  This can be locally changed with `with jax.transfer_guard('allow'):`

  Yields:
    None
  """
  # Only activate this inside the train loop as there's issues like:
  # https://github.com/google/jax/issues/16002
  if not jax.config.jax_disable_jit:
    with jax.transfer_guard("disallow"):
      yield
  else:
    yield


def _sync():
  """Syncs hosts and empties async computation queue."""

  def _psync(x):
    return jax.lax.psum(x, "i")

  with jax.transfer_guard("allow"):
    x = jnp.ones([jax.local_device_count()])
    return jax.pmap(_psync, "i")(x).block_until_ready()
