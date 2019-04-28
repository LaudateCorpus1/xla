from __future__ import print_function

import collections
import gc
from six import itervalues
import os
import re
import threading
import time
import torch
import torch.nn as nn
import torch_xla
import torch_xla_py.graph_saver as gs
import torch_xla_py.utils as xu
import torch_xla_py.keyd_queue as kq

_XLA_DEVICES = torch_xla._XLAC._xla_get_devices()


def is_xla_tensor(tensor):
  return tensor.device.type == 'xla'


def get_xla_supported_devices(devkind=None):
  devkind = devkind or ['TPU', 'GPU', 'CPU']
  for kind in devkind:
    kind_devices = []
    for i, device in enumerate(_XLA_DEVICES):
      if re.match(kind + r':\d+$', device):
        kind_devices.append('xla:{}'.format(i))
    if kind_devices:
      return kind_devices


def xla_device(n=None, devkind=None):
  if n is None:
    devices = get_xla_supported_devices(devkind=devkind)
    assert devices, 'No devices of {} kind'.format(devkind or 'ANY')
    return torch.device(devices[0])
  return torch.device('xla:{}'.format(n))


class OptimizerState(object):

  def __init__(self):
    self.tensors = []
    self.gradients = []


class RateTracker(object):

  def __init__(self, smooth_factor=0.8):
    self._smooth_factor = smooth_factor
    self._start_time = time.time()
    self._partial_time = self._start_time
    self._count = 0
    self._rate = 0.0

  def update(self, count):
    now = time.time()
    delta = now - self._partial_time
    if delta > 0:
      rate = (count - self._count) / delta
      self._rate = (
          self._rate * self._smooth_factor + rate * (1.0 - self._smooth_factor))
    self._partial_time = now
    self._count = count
    return self._rate

  def add(self, count):
    return self.update(self._count + count)

  def rate(self):
    return self._rate

  def global_rate(self):
    return self._count / (self._partial_time - self._start_time)


class TrainStepMetrics(object):

  LOG_FORMAT = ('Train Epoch: {} [{}/{} ({:.0f}%)]\t'
                'Loss: {:.6f}\tSamples/sec: {:.1f}')

  def __init__(self, epoch, num_cores, batch_number, num_batches, batch_size,
               loss, examples_per_sec, global_step):
    """Constructor for the metrics of a single train step.

    Args:
      epoch: The current epoch number.
      num_cores: The number of cores on which model is being trained.
      batch_number: The current batch number. Reset to 0 every epoch.
      num_batches: The number of batches in a single epoch.
      batch_size: Per core batch size.
      loss: Training loss.
      examples_per_sec: The number of processed samples per second.
      global_step: The global step number of current batch.
    """
    self._epoch = epoch
    self._processed_samples = num_cores * (batch_number + 1) * batch_size
    self._dataset_size = num_batches * batch_size
    self._percent_epoch_done = 100. * batch_number * num_cores / num_batches
    self._loss = loss
    self._examples_per_sec = examples_per_sec
    self._global_step = global_step
    self._global_step_per_sec = examples_per_sec / batch_size

  def write_summary(self, writer):
    if writer:
      writer.add_scalar('loss', self._loss, self._global_step)
      writer.add_scalar('global_step/sec', self._global_step_per_sec,
                        self._global_step)

  def __repr__(self):
    return self.LOG_FORMAT.format(self._epoch, self._processed_samples,
                                  self._dataset_size, self._percent_epoch_done,
                                  self._loss, self._examples_per_sec)


class TestStepMetrics(object):

  LOG_FORMAT = ('\nTest set: Average loss: {:.4f}, Accuracy: {}/{} ({:.2f}%), '
                'Samples/sec: {:.1f}\n')

  def __init__(self, loss, correct, count, examples_per_sec, global_step):
    """Constructor for the metrics of a single test step.

    Args:
      loss: The test loss.
      correct: The number of correct samples.
      count: Total number of samples.
      examples_per_sec: The number of processed samples per second.
      global_step: The global step number of current batch.
    """
    self._loss = loss
    self._correct = correct
    self._total = count
    self._global_step = global_step
    self._accuracy = 100.0 * correct / count
    self._examples_per_sec = examples_per_sec

  def write_summary(self, writer):
    if writer:
      writer.add_scalar('accuracy', self._accuracy, self._global_step)

  def __repr__(self):
    return self.LOG_FORMAT.format(self._loss, self._correct, self._total,
                                  self._accuracy, self._examples_per_sec)


class LinearIndex(object):

  def __init__(self, index):
    self.index = index


class ToXlaTensorArena(object):

  def __init__(self, convert_fn):
    self.convert_fn = convert_fn
    self._tensors = []
    self._devices = []
    self._converted_tensors = None

  def add(self, tensor, device=None):
    if self._tensors:
      assert type(self._tensors[0]) == type(tensor)
    self._tensors.append(tensor)
    if device is not None:
      self._devices.append(device)
    return LinearIndex(len(self._tensors) - 1)

  def convert(self):
    if self._tensors:
      self._converted_tensors = self.convert_fn(self._tensors, self._devices)

  def get_converted_tensor(self, lindex):
    assert isinstance(lindex, LinearIndex)
    assert self._converted_tensors is not None
    assert lindex.index < len(self._converted_tensors)
    return self._converted_tensors[lindex.index]


def _collect_tensors(arena, collect_type, inputs, devices=None, device=''):
  if type(inputs) == collect_type:
    return arena.add(inputs, device=device)
  if isinstance(inputs, (list, tuple)):
    tensors = []
    for i, input in enumerate(inputs):
      if devices is not None:
        # The first dimension, if devices is specified, is the replica
        # dimension, and we assign every nested tensor to the proper
        # replica device.
        assert len(devices) > i
        device = devices[i]
      tensors.append(
          _collect_tensors(arena, collect_type, input, device=device))
    return tuple(tensors)
  return inputs


def _replace_tensors(arena, tensors):
  if isinstance(tensors, LinearIndex):
    return arena.get_converted_tensor(tensors)
  if isinstance(tensors, (list, tuple)):
    new_tensors = []
    for tensor in tensors:
      new_tensors.append(_replace_tensors(arena, tensor))
    return tuple(new_tensors)
  return tensors


def _get_summary_writer(logdir=None):
  if logdir:
    from tensorboardX import SummaryWriter
    return SummaryWriter(logdir)


def get_log_fn(logdir=None, custom_log_fn=print):
  writer = _get_summary_writer(logdir)

  def log_fn(step_result):
    if (isinstance(step_result, TrainStepMetrics) or
        isinstance(step_result, TestStepMetrics)):
      step_result.write_summary(writer)
      custom_log_fn(str(step_result))
    else:
      custom_log_fn(step_result)

  return log_fn


def _fetch_optimizer_state(optimizer):

  def add(p, state):
    if isinstance(p, torch.Tensor):
      state.tensors.append(p.data)
      if p.grad is not None:
        state.gradients.append(p.grad.data)
      pstate = optimizer.state.get(p, None)
      if pstate:
        add(pstate, state)
    elif isinstance(p, dict):
      for k, v in p.items():
        add(k, state)
        add(v, state)
    elif isinstance(p, (list, tuple, set)):
      for x in p:
        add(x, state)

  state = OptimizerState()
  add(optimizer.__getstate__(), state)
  return state


def _mark_step(state):
  save_dir = os.environ.get('SAVE_GRAPH_DIR', None)
  if save_dir:
    gs.save_tensors_graph(save_dir, 'optimizer_step',
                          state.gradients + state.tensors)
  torch_xla._XLAC._xla_step_marker(torch_xla._XLAC._xla_get_default_device())


def optimizer_step(optimizer, closure=None):
  state = _fetch_optimizer_state(optimizer)
  count = torch_xla._XLAC._xla_replication_device_count()
  if count > 1:
    torch_xla._XLAC._xla_cross_replica_sum(state.gradients, 1.0 / count, [])
  loss = optimizer.step(closure=closure)
  _mark_step(state)
  return loss
