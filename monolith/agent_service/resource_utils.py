# Copyright 2022 ByteDance and/or its affiliates.
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

from absl import logging
import os
import time
import subprocess
import re
import psutil
from google.protobuf import text_format

import tensorflow as tf
from typing import Dict, Union, List

import monolith.agent_service.utils
from monolith.agent_service.data_def import SubModelName, VersionPath, SubModelSize

from monolith.native_training.model_export import export_pb2

_HADOOP_BIN = '/opt/tiger/yarn_deploy/hadoop/bin/hadoop'
ROW = re.compile(r"^.+_(\d+)-\d+-of-\d+$")
_ExportSaverListenerStateFile = 'ExportSaverListenerState'


def _get_pod_cgroup_path():
  cmd = ["cat", "/proc/1/cgroup"]
  try:
    out_bytes = subprocess.check_output(cmd)
    out_list = out_bytes.decode('utf-8').strip().split('\n')
    for line in out_list:
      if ':memory:' in line:
        return line.strip().split(':')[-1].strip('/')
  except Exception as e:
    return None


_POD_CGROUP_PATH = _get_pod_cgroup_path()


def exists(dirname: str) -> bool:
  cmd = [_HADOOP_BIN, 'fs', '-test', '-e', dirname]
  try:
    out_bytes = subprocess.check_output(cmd)
    out_list = out_bytes.decode('utf-8')
    return len(out_list) == 0
  except Exception as e:
    logging.info(f"{dirname} is not exists: {e}")
    return False


def disk_usage(dirname: Union[str, List[str]],
               tatol: bool = False) -> Dict[str, int]:
  if tatol:
    cmd = [_HADOOP_BIN, 'fs', '-du', '-s']
  else:
    cmd = [_HADOOP_BIN, 'fs', '-du']
  if isinstance(dirname, (list, tuple)):
    cmd.extend(dirname)
  else:
    if not exists(dirname):
      return None
    cmd.append(dirname)
  cnt, max_try = 0, 3
  while cnt < max_try:
    try:
      out_bytes = subprocess.check_output(cmd)
      out_list = out_bytes.decode('utf-8').strip().split('\n')
      ROW = re.compile(r"^(\d+)\s+\d+\s+(.+)")
      result = {}
      for line in out_list:
        matched = ROW.match(line)
        if matched:
          size = int(matched.group(1))
          name = matched.group(2)
          result[name] = size
      return result
    except Exception as e:
      logging.info(f"disk_usage: {e}")
      cnt += 1
  return None


def open_hdfs(fname: Union[str, List[str]]):
  cmd = [_HADOOP_BIN, 'fs', '-text']
  if isinstance(fname, (list, tuple)):
    cmd.extend(fname)
  else:
    cmd.append(fname)
  out_list = None
  cnt, max_try = 0, 3
  while cnt < max_try:
    try:
      out_bytes = subprocess.check_output(cmd)
      out_list = out_bytes.decode('utf-8').strip().split('\n')
      break
    except Exception as e:
      logging.info(e)
      cnt += 1
  assert out_list is not None
  for line in out_list:
    line = line.strip()
    if len(line) > 0:
      yield line


def _ckpt_parser(fname):
  result = {"model_checkpoint_path": None, "all_model_checkpoint_paths": []}

  for line in open_hdfs(fname):
    key, value = line.split(':')
    key, value = key.strip(), value.strip().strip('"')
    if key.startswith('all'):
      result[key].append(os.path.basename(value))
    else:
      result[key] = os.path.basename(value)
  return result


def _export_saver_listener_state_parser(fname: Union[str, List[str]]):
  results = []
  export_dir, global_step = None, None
  for line in open_hdfs(fname):
    if line.startswith('entries'):
      if export_dir is not None and global_step is not None:
        results.append((export_dir, global_step))
    elif line.startswith('export_dir'):
      export_dir = line.split(': ')[-1].strip().strip('"')
    elif line.startswith('global_step'):
      global_step = int(line.split(':')[-1].strip())
    else:
      pass
  if export_dir is not None and global_step is not None:
    results.append((export_dir, global_step))
  stats = {}
  for export_dir, global_step in results:
    base_path = os.path.dirname(export_dir)
    basename = os.path.basename(export_dir)
    if base_path in stats:
      stats[base_path].append((basename, global_step))
    else:
      stats[base_path] = [(basename, global_step)]
  for name, values in stats.items():
    if len(values) == 0:
      return None
    values.sort(key=lambda items: items[1])
  return stats


def cal_model_info(dirname: str,
                   ckpt: str = None) -> 'Dict[SubModelName, SubModelSize]':
  # ckpt: model.ckpt-145951282
  # 1) get all the saved_model name the exported dir
  raw_info = {os.path.basename(p): s for p, s in disk_usage(dirname).items()}
  if raw_info is None:
    return None

  # 2) read checkpoint file to get ckpt name
  ckpt_base = os.path.dirname(dirname.rstrip('/'))
  logging.info(f'ckpt_base path is {ckpt_base}')
  if ckpt is None:
    ckpt_info = _ckpt_parser(os.path.join(ckpt_base, 'checkpoint'))
    ckpt = os.path.basename(ckpt_info['model_checkpoint_path'])

  # 3) get the size of each saved_model, not include assets
  state_files = []
  for name in raw_info:
    model_path = os.path.join(dirname.rstrip('/'), name)
    export_saver_listener_state = os.path.join(model_path,
                                               _ExportSaverListenerStateFile)
    state_files.append(export_saver_listener_state)
  state_dict = _export_saver_listener_state_parser(state_files)
  if state_dict is None:
    return None
  global_step = int(ckpt.split('-')[-1])
  smdirs = []
  for name, values in state_dict.items():
    gs_dict = {gs: i for i, (_, gs) in enumerate(values)}
    if global_step in gs_dict:
      smid = values[gs_dict[global_step]][0]
    else:
      smid = values[-1][0]
    smdirs.append(os.path.join(name, smid))
  stats_du = disk_usage(smdirs, tatol=True)
  info = {
      os.path.basename(os.path.dirname(p)): size
      for p, size in stats_du.items()
  }
  logging.info(f'model info without assets is {info}')

  # 4) add assets size to ps saved_models
  assets = os.path.join(ckpt_base, f'{ckpt}.assets')
  logging.info(f'assets path is {assets}')
  assets_info = disk_usage(assets)
  for key, value in assets_info.items():
    matched = ROW.match(os.path.basename(key))
    if matched is not None:
      key = f'ps_{matched.group(1)}'
      info[key] += value
  logging.info(f'model info with assets is {info}')
  return info


def get_export_saver_listener_state(
    export_dir_base: str) -> export_pb2.ServingModelState:
  filename = os.path.join(export_dir_base, _ExportSaverListenerStateFile)
  state = export_pb2.ServingModelState()
  try:
    with tf.io.gfile.GFile(filename) as f:
      text = f.read()
      text_format.Merge(text, state)
  except tf.errors.NotFoundError:
    return None
  return state


def cal_model_info_v2(
    exported_models_path: str,
    ckpt: str = None,
    version: str = None) -> 'Dict[SubModelName, (SubModelSize, VersionPath)]':
  # 1) get all names of saved_models
  if os.path.isabs(exported_models_path):
    exported_models_path = exported_models_path.rstrip('/')
  else:
    exported_models_path = os.path.abspath(exported_models_path.rstrip('/'))

  if not tf.io.gfile.exists(exported_models_path):
    raise Exception(f"{exported_models_path} is not exists ")

  model_info = {
      sub_model_name: 0
      for sub_model_name in tf.io.gfile.listdir(exported_models_path)
      if not sub_model_name.startswith('.')
  }

  # 2) ensure checkpoint
  ckpt_base_path = os.path.dirname(exported_models_path)
  if ckpt is None:
    checkpoint_state = tf.train.get_checkpoint_state(ckpt_base_path)
    if checkpoint_state is not None:
      ckpt = os.path.basename(checkpoint_state.model_checkpoint_path)
  global_step = -1 if ckpt is None else int(ckpt.split('-')[-1])

  # 3) ensure version
  if version is None:
    com_versions = None
    for sub_model_name in tf.io.gfile.listdir(exported_models_path):
      if sub_model_name.startswith('.'):
        continue
      tfs_base_path = os.path.join(exported_models_path, sub_model_name)
      state = get_export_saver_listener_state(tfs_base_path)
      if global_step >= 0 and state is not None:
        versions = set()
        for se in state.entries:
          _version = int(os.path.basename(se.export_dir))
          versions.add(_version)
          if se.global_step == global_step:
            if version is None:
              version = _version
            else:
              assert version == _version
            break
      else:
        versions = set(
            int(num)
            for num in tf.io.gfile.listdir(tfs_base_path)
            if num.isnumeric())

      if com_versions is None:
        com_versions = versions
      else:
        com_versions &= versions

    assert com_versions is not None and len(com_versions) > 0
    version = version or sorted(com_versions)[-1]
  else:
    version = int(version)

  # 4) get dense part size of all saved_models
  for sub_model_name in model_info:
    version_path = os.path.join(exported_models_path, sub_model_name,
                                str(version))
    assert tf.io.gfile.exists(version_path)
    for (dir_name, _, file_names) in tf.io.gfile.walk(version_path):
      for fn in file_names:
        stat = tf.io.gfile.stat(os.path.join(dir_name, fn))
        model_info[sub_model_name] += stat.length

  # 5) add assets length (sparse part size)
  assets_path = os.path.join(ckpt_base_path, f'{ckpt}.assets')
  if tf.io.gfile.exists(assets_path):
    for fn in tf.io.gfile.listdir(assets_path):
      matched = ROW.match(fn)
      if matched:
        key = f'ps_{matched.group(1)}'
        stat = tf.io.gfile.stat(os.path.join(assets_path, fn))
        model_info[key] += stat.length

  return {
      sub_model_name:
      (size, os.path.join(exported_models_path, sub_model_name, str(version)))
      for sub_model_name, size in model_info.items()
  }


def total_memory() -> int:
  memory_base = os.path.join("/sys/fs/cgroup/memory", _POD_CGROUP_PATH)
  limit_in_bytes = 0
  with open(os.path.join(memory_base, 'memory.limit_in_bytes'), 'r') as stream:
    for line in stream:
      limit_in_bytes = int(line.strip())

  if limit_in_bytes == 0:
    return int(os.environ.get('MY_MEM_LIMIT'))
  else:
    return limit_in_bytes


def total_memory_v2() -> int:
  mem = psutil.virtual_memory()
  return mem.total


def cal_available_memory() -> int:
  memory_base = os.path.join("/sys/fs/cgroup/memory", _POD_CGROUP_PATH)
  usage_in_bytes = 0
  with open(os.path.join(memory_base, 'memory.usage_in_bytes'), 'r') as stream:
    for line in stream:
      usage_in_bytes = int(line.strip())

  limit_in_bytes = 0
  with open(os.path.join(memory_base, 'memory.limit_in_bytes'), 'r') as stream:
    for line in stream:
      limit_in_bytes = int(line.strip())
  return limit_in_bytes - usage_in_bytes


def cal_available_memory_v2() -> int:
  mem = psutil.virtual_memory()
  return mem.available


class CPU(object):

  def __init__(self, cpuacct_file):
    self.cpuacct_file = cpuacct_file
    self.last_wall_clock = self.wall_clock()
    self.last_cpu_clock = self.cpu_clock()

  def wall_clock(self):
    try:
      # time_ns() only supported by python 3.7
      total_time = time.time_ns()
    except Exception as e:
      total_time = subprocess.check_output(['date', '+%s%N'])

    return int(total_time)

  def cpu_clock(self):
    with open(self.cpuacct_file, 'r') as f:
      use_time = int(f.read())
    return use_time

  def cpu_usage(self):
    current_wall_clock = self.wall_clock()
    current_cpu_clock = self.cpu_clock()

    delta_cpu_time = current_cpu_clock - self.last_cpu_clock
    delta_wall_time = current_wall_clock - self.last_wall_clock
    usage = delta_cpu_time / delta_wall_time

    self.last_wall_clock = current_wall_clock
    self.last_cpu_clock = current_cpu_clock

    return usage


def num_cpu():
  cpu_base = os.path.join("/sys/fs/cgroup/cpu", _POD_CGROUP_PATH)
  cfs_quota_us = 0
  with open(os.path.join(cpu_base, 'cpu.cfs_quota_us'), 'r') as stream:
    for line in stream:
      cfs_quota_us = int(line.strip())

  cfs_period_us = 0
  with open(os.path.join(cpu_base, 'cpu.cfs_period_us'), 'r') as stream:
    for line in stream:
      cfs_period_us = int(line.strip())

  if cfs_period_us == 0:
    return int(os.environ.get('MY_CPU_LIMIT'))
  else:
    return int(cfs_quota_us / cfs_period_us)


def cal_cpu_usage():
  cpu_base = os.path.join("/sys/fs/cgroup/cpu", _POD_CGROUP_PATH)
  cpuacct_file = os.path.join(cpu_base, 'cpuacct.usage')
  cpu = CPU(cpuacct_file)
  cpu_usages, cnt, max_try = [], 0, 5
  while cnt < max_try:
    time.sleep(1)
    cpu_usages.append(round(cpu.cpu_usage() * 100, 2))
    cnt += 1
  return sum(cpu_usages) / max_try


def cal_cpu_usage_v2() -> float:
  return psutil.cpu_percent()
