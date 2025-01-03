"""
Copyright 2023 Google LLC

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

     https://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

# pytype: skip-file
# pylint: disable=missing-module-docstring, bare-except, consider-using-generator, missing-function-docstring
from collections import OrderedDict
import math
import os
import sys
from typing import Any, Union

import jax
from jax.experimental.compilation_cache import compilation_cache
from layers.attentions import AttentionType
import accelerator_to_spec_map
import max_logging
import max_utils
import yaml

# pylint: disable=line-too-long

_MAX_PREFIX = "M_"

# YAML attribute to specify inheritance.
_BASE_CONFIG_ATTR = "base_config"


def yaml_key_to_env_key(s: str) -> str:
  return _MAX_PREFIX + s.upper()


def string_to_bool(s: str) -> bool:
  if s.lower() == "true":
    return True
  if s.lower() == "false":
    return False
  raise ValueError(f"Can't convert {s} to bool")


_yaml_types_to_parser = {str: str, int: int, float: float, bool: string_to_bool}


def validate_compute_axis_order(s: str) -> None:
  valid_compute_axis_order = ("0,1,2,3", "0,2,1,3")
  if s not in valid_compute_axis_order:  # currently supported compute_axis_order
    raise ValueError("Invalid compute_axis_order was passed. Valid options ", valid_compute_axis_order)


def validate_kv_quant_axis(s: str, quantize_kvcache: bool) -> None:
  valid_kv_quant_axis = ("", "dkv", "heads_and_dkv")
  if s not in valid_kv_quant_axis:  # currently supported kv_quant_axis
    raise ValueError("Invalid kv_quant_axis was passed. Valid options ", valid_kv_quant_axis)
  if quantize_kvcache and s == "":
    raise ValueError("kv_quant_axis can not be '' when quantize_kvcache is True")


def validate_attention_kernel(s: str) -> None:
  valid_attention_kernels = ("autoselected", "dot_product", "flash", "cudnn_flash_te")
  if s not in valid_attention_kernels:  # currently supported attention
    raise ValueError("Invalid attention kernel was passed. Valid options ", valid_attention_kernels)


def validate_attention_type(s: str) -> None:
  valid_attention_types = (attention_type.value for attention_type in AttentionType)
  if s not in valid_attention_types:  # currently supported attention
    raise ValueError("Invalid attention type was passed. Valid options ", valid_attention_types)


def validate_profiler_type(s: str) -> None:
  valid_profiler_types = ("", "nsys", "xplane")
  if s not in valid_profiler_types:  # currently supported attention
    raise ValueError("Invalid profiler type was passed. Valid options ", valid_profiler_types)


def validate_model_call_mode(s: str) -> None:
  valid_model_call_modes = ("", "inference")
  if s not in valid_model_call_modes:  # currently supported attention
    raise ValueError(f"Invalid model call mode {s}. Valid options are {valid_model_call_modes}")


def validate_prefill_and_target_lengths(max_prefill_length: int, max_target_length: int) -> None:
  if max_prefill_length <= 0:
    raise ValueError(f"Invalid max_prefill_predict_length {max_prefill_length}, it should be a positive number")
  if max_target_length <= max_prefill_length:
    raise ValueError(
        f"Invalid max_target_length {max_target_length}, this should be sum of "
        f"max_prefill_predict_length ({max_prefill_length}) and max output length expected."
    )


def validate_keys(keys):
  validate_attention_kernel(keys["attention"])
  validate_attention_type(keys["attention_type"])
  validate_profiler_type(keys["profiler"])
  validate_compute_axis_order(keys["compute_axis_order"])
  validate_kv_quant_axis(keys["kv_quant_axis"], keys["quantize_kvcache"])
  validate_model_call_mode(keys["model_call_mode"])
  validate_prefill_and_target_lengths(keys["max_prefill_predict_length"], keys["max_target_length"])

  assert (keys["load_parameters_path"] == "" and keys["load_full_state_path"] == "") or keys[
      "enable_checkpointing"
  ], "You must set enable_checkpointing to load a checkpoint"
  assert (
      keys["load_parameters_path"] == "" or keys["load_full_state_path"] == ""
  ), "At most one of `load_parameters_path` or `load_full_state_path` should be set"
  if keys["enable_emergency_checkpoint"]:
    assert (
        keys["local_checkpoint_directory"] != ""
    ), "A local checkpoint directory must be specified when using emergency checkpoint"
    assert (
        keys["local_checkpoint_period"] > 0
    ), "A positive local checkpoint period must be specified when using emergency checkpoint"
    if keys["use_replicator_service"]:
      assert (
          keys["replicator_backup_interval_minutes"] > 0
      ), "Replicator service is enabled, the backup interval minutes must be positive"
  else:
    max_logging.log(
        "Not using emergency checkpoint, ignoring local_checkpoint_directory, local_checkpoint_period,"
        " use_replicator_service and replicator_backup_interval_minutes"
    )

  validate_multiple_slices(keys)
  if keys["num_experts"] > 1:
    validate_megablox_parallelism(keys)


def validate_data_input(keys):
  """validate provided parameters for data input"""
  if keys["dataset_type"] == "hf":
    max_logging.log(
        f"dataset_type set to hf, will use {keys['hf_path']=}, {keys['hf_data_dir']=} and {keys['hf_train_files']=} to read data"
    )
    assert keys["hf_path"] != "", "hf_path can't be empty when dataset_type=hf"
    if not keys["hf_train_files"]:
      keys["hf_train_files"] = None
    if not keys["hf_eval_files"]:
      keys["hf_eval_files"] = None
    if keys["hf_eval_files"]:
      keys["hf_eval_split"] = "train"
    if keys["eval_interval"] > 0:
      assert keys["hf_eval_split"], "Please specify hf_eval_split or set eval_interval to <=0."

  elif keys["dataset_type"] == "grain":
    max_logging.log(
        f"dataset_type set to grain, will use {keys['grain_train_files']=} as data files, and {keys['grain_worker_count']} workers"
    )
    assert keys["grain_train_files"] != "", "grain_train_files can't be empty when dataset_type=grain"
    if keys["eval_interval"] > 0:
      assert keys["grain_eval_files"], "Please specify grain_eval_files or set eval_interval to <=0."
  elif keys["dataset_type"] == "tfds":
    max_logging.log(f"dataset_type set to tfds, will use {keys['dataset_path']=} and {keys['dataset_name']=}")
    assert keys["dataset_name"] != "", "dataset_name can't be empty when dataset_type=tfds"
    if keys["eval_interval"] > 0:
      assert keys["eval_split"], "Please specify eval_split or set eval_interval to <=0."

  if keys["sharding_tolerance"] > 1.0 or keys["sharding_tolerance"] < 0.0:
    max_logging.log(
        "WARNING: 'sharding_tolerance: allowed percentage of non-sharded parameters' should be between 0.0 and 1.0"
    )


def validate_model_name(s: str) -> bool:
  """Validate provided model name."""
  # currently supported models
  valid_model_names = (
      "default",
      "llama2-7b",
      "llama2-13b",
      "llama2-70b",
      "llama3-8b",
      "llama3-70b",
      "llama3.1-8b",
      "llama3.1-70b",
      "llama3.1-405b",
      "mistral-7b",
      "mixtral-8x7b",
      "mixtral-8x22b",
      "gemma-7b",
      "gemma-2b",
      "gemma2-2b",
      "gemma2-9b",
      "gemma2-27b",
      "gpt3-175b",
      "gpt3-22b",
      "gpt3-6b",
      "gpt3-52k",
  )
  if s not in valid_model_names:
    raise ValueError(f"Invalid model name was passed. Got {s}, Valid options {valid_model_names}")


def validate_no_keys_overwritten_twice(keys1: list[str], keys2: list[str]):
  overwritten_keys = [k for k in keys1 if k in keys2]
  if overwritten_keys:
    raise ValueError(
        f"Keys {overwritten_keys} are overwritten from both the model"
        " and the environment/command line. This isn't allowed."
    )


def validate_and_assign_remat_tensors(keys):
  # list of allowed tensors for custom remat policy
  tensors = [
      "decoder_layer_input",
      "context",
      "mlpwi",
      "mlpwi_0",
      "mlpwi_1",
      "mlpwo",
      "query_proj",
      "key_proj",
      "value_proj",
      "out_proj",
  ]
  assert keys["decoder_layer_input"] != "remat", "Cannot remeterialize this tensor with scan_layers=True"
  tensors_on_device = []
  tensors_to_offload = []
  for t in tensors:
    if keys[t] == "device":
      tensors_on_device.append(t)
    elif keys[t] == "offload":
      tensors_to_offload.append(t)
    elif keys[t] != "remat":
      raise ValueError(f"Invalid value chosen for tensor {t}")
  keys["tensors_on_device"] = tensors_on_device
  keys["tensors_to_offload"] = tensors_to_offload
  return keys


_config = None
config = None


def _lists_to_tuples(l: list[Any]) -> Union[tuple[Any], list[Any]]:
  return tuple(_lists_to_tuples(x) for x in l) if isinstance(l, list) else l


class _HyperParameters:
  # pylint: disable=missing-class-docstring
  def _validate_env_variables(self, raw_data_from_yaml: dict[str, Any]):
    for environment_var in os.environ:
      if environment_var[: len(_MAX_PREFIX)] == _MAX_PREFIX:
        proposed_key = environment_var[len(_MAX_PREFIX) :].lower()
        if proposed_key not in raw_data_from_yaml:
          raise ValueError(f"We received env `{environment_var}` but it doesn't match a key, so it is assumed a mistake.")
        if not environment_var[len(_MAX_PREFIX) :].isupper():
          raise ValueError(f"We received env `{environment_var}` but it isn't all uppercase.")

  def _load_kwargs(self, argv: list[str], **kwargs):
    args_dict = dict(a.split("=", 1) for a in argv[2:])
    args_dict.update(kwargs)
    return args_dict

  def _update_from_env_and_command_line(self, raw_keys, raw_data_from_yaml, argv, **kwargs) -> list[str]:
    """Update model config from environment and command line"""
    raw_data_from_cmd_line = self._load_kwargs(argv, **kwargs)
    updated_keys = []

    for k in raw_data_from_cmd_line:
      if k not in raw_data_from_yaml:
        raise ValueError(f"Key {k} was passed at the command line but isn't in config.")

    for k in raw_data_from_yaml:
      if k in raw_data_from_cmd_line and yaml_key_to_env_key(k) in os.environ:
        raise ValueError(f"You are passing overrides by both CLI and ENV for `{k}`. This isn't allowed.")

      if not k in raw_data_from_cmd_line and not yaml_key_to_env_key(k) in os.environ:
        raw_keys[k] = raw_data_from_yaml[k]
        continue

      updated_keys.append(k)
      if k in raw_data_from_cmd_line:
        new_proposal = raw_data_from_cmd_line[k]
      else:
        new_proposal = os.environ.get(yaml_key_to_env_key(k))

      if (not isinstance(new_proposal, type(raw_data_from_yaml[k]))) and (
          type(raw_data_from_yaml[k]) not in _yaml_types_to_parser
      ):
        raise ValueError(
            f"For key '{k}', type {type(raw_data_from_yaml[k])} not in {_yaml_types_to_parser.keys()}, can't pass"
            " at the CLI or ENV"
        )

      if isinstance(new_proposal, type(raw_data_from_yaml[k])):
        raw_keys[k] = new_proposal  # take the raw data, no type conversion
      else:
        try:
          raw_keys[k] = _yaml_types_to_parser[type(raw_data_from_yaml[k])](
              new_proposal
          )  # take the command line value, but type it like the config value.
        except ValueError as e:
          raise ValueError(f"Couldn't parse value from CLI or ENV '{new_proposal}' for key '{k}'") from e

    return updated_keys

  def _load_config(self, config_name: str) -> dict[str, Any]:
    """Loads the YAML config from a file with a given name."""
    with open(config_name, "r", encoding="utf-8") as yaml_file:
      raw_data_from_yaml = yaml.safe_load(yaml_file)

    # Load data from parent config. Note that inheritance has override
    # semantics, and the path is relative to the current config.
    if _BASE_CONFIG_ATTR in raw_data_from_yaml:
      parent_config_filename = raw_data_from_yaml[_BASE_CONFIG_ATTR]
      if not os.path.isabs(parent_config_filename):
        loaded_parent_config_filename = os.path.join(os.path.dirname(config_name), parent_config_filename)
        if not os.path.isfile(loaded_parent_config_filename):
          dir_path = os.path.dirname(os.path.realpath(__file__))
          loaded_parent_config_filename = os.path.join(dir_path, f"configs/{parent_config_filename}")
      else:
        loaded_parent_config_filename = parent_config_filename

      base_config = self._load_config(loaded_parent_config_filename)
      for key, value in raw_data_from_yaml.items():
        base_config[key] = value
      return base_config
    return raw_data_from_yaml

  def __init__(self, argv: list[str], **kwargs):
    config_name: str = argv[1]
    raw_data_from_yaml = self._load_config(config_name)

    self._validate_env_variables(raw_data_from_yaml)

    raw_keys = OrderedDict()
    keys_from_env_and_command_line = self._update_from_env_and_command_line(raw_keys, raw_data_from_yaml, argv, **kwargs)
    max_logging.log(f"Updating keys from env and command line: {keys_from_env_and_command_line}")
    keys_from_model = _HyperParameters.update_model_vars(argv[1], raw_keys, config_name)
    max_logging.log(f"Updating keys from model: {keys_from_model}")
    validate_no_keys_overwritten_twice(keys_from_env_and_command_line, keys_from_model)

    # This must be invoked before initializing the backend
    raw_keys = validate_and_set_hlo_dump_defaults(raw_keys)

    # We initialize the jax distributed system here because it must be done before device backend is initialized.
    if raw_keys["jax_debug_log_modules"]:
      jax.config.update("jax_debug_log_modules", raw_keys["jax_debug_log_modules"])
    max_utils.maybe_initialize_jax_distributed_system(raw_keys)

    if raw_keys["jax_cache_dir"]:
      compilation_cache.set_cache_dir(os.path.expanduser(raw_keys["jax_cache_dir"]))

    _HyperParameters.user_init(raw_keys)
    if raw_keys["dataset_type"] == "c4_mlperf" and raw_keys["model_name"] == "gpt3-175b":
      _HyperParameters.configure_gpt3_task(raw_keys)

    if not os.path.isfile(raw_keys["tokenizer_path"]):
      # Try and find the tokenizer path relative to the config file.
      tokenizer_path = os.path.join(
          os.path.dirname(config_name),
          raw_keys["tokenizer_path"],
      )

      if os.path.isfile(tokenizer_path):
        raw_keys["tokenizer_path"] = tokenizer_path

    self.keys = raw_keys
    keys = [k for k in raw_keys]  # pylint: disable=unnecessary-comprehension
    keys.sort()

    if raw_keys["log_config"]:
      for k in keys:
        max_logging.log(f"Config param {k}: {raw_keys[k]}")

  @staticmethod
  def user_init(raw_keys):
    """Transformations between the config data and configs used at runtime"""
    if raw_keys["run_name"] == "":
      raw_keys["run_name"] = os.environ.get("JOBSET_NAME")  # using XPK default
    run_name = raw_keys["run_name"]
    base_output_directory = raw_keys["base_output_directory"]
    if run_name:
      raw_keys["tensorboard_dir"] = os.path.join(base_output_directory, run_name, "tensorboard", "")
      raw_keys["checkpoint_dir"] = os.path.join(base_output_directory, run_name, "checkpoints", "")
      raw_keys["metrics_dir"] = os.path.join(base_output_directory, run_name, "metrics", "")

    if raw_keys["learning_rate_schedule_steps"] == -1:
      raw_keys["learning_rate_schedule_steps"] = raw_keys["steps"]
    if raw_keys["steps"] == -1:
      raw_keys["steps"] = raw_keys["learning_rate_schedule_steps"]

    if raw_keys["attn_logits_soft_cap"] == 0.0:
      raw_keys["attn_logits_soft_cap"] = None
    if raw_keys["final_logits_soft_cap"] == 0.0:
      raw_keys["final_logits_soft_cap"] = None

    emb_scale, num_head_scale, mlp_dim_scale, layer_scale = get_individual_scales(raw_keys["global_parameter_scale"])
    raw_keys["emb_dim"] = 2**emb_scale * raw_keys["base_emb_dim"]
    raw_keys["num_query_heads"] = 2**num_head_scale * raw_keys["base_num_query_heads"]
    raw_keys["num_kv_heads"] = 2**num_head_scale * raw_keys["base_num_kv_heads"]
    raw_keys["mlp_dim"] = 2**mlp_dim_scale * raw_keys["base_mlp_dim"]
    raw_keys["num_decoder_layers"] = 2**layer_scale * raw_keys["base_num_decoder_layers"]

    # This is the first command that initializes the backend - it calls
    # jax.devices()
    (
        raw_keys["global_batch_size_to_load"],
        raw_keys["global_batch_size_to_train_on"],
        raw_keys["micro_batch_size_to_train_on"],
    ) = calculate_global_batch_sizes(
        raw_keys["per_device_batch_size"],
        raw_keys["expansion_factor_real_data"],
        get_num_target_devices(raw_keys),
        raw_keys["gradient_accumulation_steps"],
    )

    if raw_keys["eval_per_device_batch_size"] <= 0:
      raw_keys["eval_per_device_batch_size"] = raw_keys["per_device_batch_size"]

    (
        raw_keys["global_batch_size_to_load_eval"],
        raw_keys["global_batch_size_to_eval_on"],
        raw_keys["micro_batch_size_to_eval_on"],
    ) = calculate_global_batch_sizes(
        raw_keys["eval_per_device_batch_size"], raw_keys["expansion_factor_real_data"], get_num_target_devices(raw_keys), 1
    )

    raw_keys["num_slices"] = max_utils.get_num_slices(raw_keys)
    raw_keys["quantization_local_shard_count"] = get_quantization_local_shard_count(raw_keys)
    raw_keys = create_parallelisms_list(raw_keys)
    raw_keys = set_and_validate_pipeline_config(raw_keys)

    if raw_keys["dataset_type"] == "c4_mlperf":
      raw_keys["add_bos"] = False
      raw_keys["add_eos"] = False
      max_logging.log("Override add_bos and add_eos to False when dataset_type=c4_mlperf")

    # Write raw_keys to GCS before type conversions
    max_utils.write_config_raw_keys_for_gcs(raw_keys)

    # Type conversions
    raw_keys["dtype"] = jax.numpy.dtype(raw_keys["dtype"])
    raw_keys["logical_axis_rules"] = _lists_to_tuples(raw_keys["logical_axis_rules"])
    raw_keys["data_sharding"] = _lists_to_tuples(raw_keys["data_sharding"])

    if raw_keys["remat_policy"] == "custom":
      raw_keys = validate_and_assign_remat_tensors(raw_keys)
    validate_keys(raw_keys)
    validate_data_input(raw_keys)

  @staticmethod
  def configure_gpt3_task(raw_keys):
    """dynamically configure gpt3 task based on training rules"""
    # follow https://github.com/google/paxml/blob/19db52eed85ae0d2365339b83a97cd0b873bbf73/paxml/tasks/lm/params/c4.py#L280
    #   according to training_rules of mlperf gpt3 training
    global_batch_size_to_train_on = raw_keys["global_batch_size_to_train_on"]
    if global_batch_size_to_train_on <= 3584:
      raw_keys["learning_rate"] = 2e-5
    else:
      raw_keys["learning_rate"] = 3e-5
    warmup_steps = math.ceil(265.0 * 1536 / global_batch_size_to_train_on - 1e-6)
    decay_end_step = math.ceil(108600.0 * 1536 / global_batch_size_to_train_on - 1e-6)
    raw_keys["learning_rate_schedule_steps"] = decay_end_step
    raw_keys["warmup_steps_fraction"] = warmup_steps / decay_end_step
    raw_keys["eval_interval"] = math.ceil(24567 / global_batch_size_to_train_on)

  @staticmethod
  def update_model_vars(base_config_path, raw_keys, config_name: str):
    """Update model config variables"""
    validate_model_name(raw_keys["model_name"])
    max_logging.log(f"Running Model: {raw_keys['model_name']}")

    updated_keys = []
    if raw_keys["model_name"] != "default":
      model_name = raw_keys["model_name"]
      # First look at the model configs next to the base_config_path, and
      # fallback to the python codebase if the config cannot be found.
      file_path = os.path.join(os.path.dirname(base_config_path), f"models/{model_name}.yml")
      if not os.path.isfile(file_path):
        dir_path = os.path.dirname(os.path.realpath(__file__))
        file_path = os.path.join(dir_path, f"configs/models/{model_name}.yml")
      with open(file_path, "r", encoding="utf-8") as file:
        model_vars = yaml.safe_load(file)
        updated_keys = list(model_vars.keys())
      raw_keys = validate_and_update_keys(raw_keys, model_vars, config_name)
    return updated_keys


def create_parallelisms_list(raw_keys):
  ici_parallelism = [
      raw_keys["ici_data_parallelism"],
      raw_keys["ici_pipeline_parallelism"],
      raw_keys["ici_fsdp_parallelism"],
      raw_keys["ici_fsdp_transpose_parallelism"],
      raw_keys["ici_sequence_parallelism"],
      raw_keys["ici_tensor_parallelism"],
      raw_keys["ici_expert_parallelism"],
      raw_keys["ici_autoregressive_parallelism"],
  ]
  dcn_parallelism = [
      raw_keys["dcn_data_parallelism"],
      raw_keys["dcn_pipeline_parallelism"],
      raw_keys["dcn_fsdp_parallelism"],
      raw_keys["dcn_fsdp_transpose_parallelism"],
      raw_keys["dcn_sequence_parallelism"],
      raw_keys["dcn_tensor_parallelism"],
      raw_keys["dcn_expert_parallelism"],
      raw_keys["dcn_autoregressive_parallelism"],
  ]
  raw_keys["ici_parallelism"] = ici_parallelism
  raw_keys["dcn_parallelism"] = dcn_parallelism
  return raw_keys


def validate_and_set_hlo_dump_defaults(raw_keys):
  if not raw_keys["dump_hlo"]:
    return raw_keys
  if os.environ.get("XLA_FLAGS") and raw_keys["dump_hlo_xla_flags"]:
    raise ValueError("You must set either XLA_FLAGS or dump_hlo_xla_flags to dump HLO, but not both.")
  if not os.environ.get("XLA_FLAGS") and not raw_keys["dump_hlo_xla_flags"]:
    raw_keys["dump_hlo_xla_flags"] = f"--xla_dump_to={raw_keys['dump_hlo_local_dir']} --xla_dump_large_constants"
    if raw_keys["dump_hlo_module_name"]:
      raw_keys["dump_hlo_xla_flags"] = (
          f"{raw_keys['dump_hlo_xla_flags']} --xla_dump_hlo_module_re={raw_keys['dump_hlo_module_name']}"
      )
  if not raw_keys["dump_hlo_gcs_dir"]:
    raw_keys["dump_hlo_gcs_dir"] = os.path.join(raw_keys["base_output_directory"], raw_keys["run_name"], "xla_dump")
  else:
    raw_keys["dump_hlo_gcs_dir"] = max_utils.add_trailing_slash(raw_keys["dump_hlo_gcs_dir"])
  if not os.environ.get("XLA_FLAGS"):
    os.environ["XLA_FLAGS"] = raw_keys["dump_hlo_xla_flags"]
  return raw_keys


def validate_multiple_slices(raw_keys):
  if (
      math.fabs(
          math.prod(
              [
                  raw_keys["dcn_data_parallelism"],
                  raw_keys["dcn_pipeline_parallelism"],
                  raw_keys["dcn_fsdp_parallelism"],
                  raw_keys["dcn_fsdp_transpose_parallelism"],
                  raw_keys["dcn_sequence_parallelism"],
                  raw_keys["dcn_tensor_parallelism"],
                  raw_keys["dcn_expert_parallelism"],
                  raw_keys["dcn_autoregressive_parallelism"],
              ]
          )
      )
      > 1
  ):
    assert raw_keys["num_slices"] > 1, "DCN parallelism requested but only one slice available."


def set_and_validate_pipeline_config(raw_keys):
  if using_pipeline_parallelism(raw_keys):

    def modify_activation_embed_and_logits_batch(logical_axis_rules):
      for idx, logical_rule in enumerate(logical_axis_rules):
        if logical_rule[0] == "activation_embed_and_logits_batch":
          # For pipeline parallelism the pre and post decoder layer tensors' batch dimension is sharded by stages.
          # Microbatches are sharded by stage, so moving out of and into this sharding should be a local reshape.
          # The "stage" needs to be listed first since the microbatch dimension is first before the reshape.
          logical_axis_rules[idx] = [
              "activation_embed_and_logits_batch",
              ["stage", "data", "fsdp", "fsdp_transpose", "expert"],
          ]
          break  # Exit the loop after modifying the list
      return logical_axis_rules

    def pipeline_first_axis(raw_keys):
      # We have seen better performance when axes used for DCN are earlier in this list than ICI, see (b/339009148) for details
      ici_parallelism = [
          raw_keys["ici_pipeline_parallelism"],
          raw_keys["ici_data_parallelism"],
          raw_keys["ici_fsdp_parallelism"],
          raw_keys["ici_fsdp_transpose_parallelism"],
          raw_keys["ici_sequence_parallelism"],
          raw_keys["ici_tensor_parallelism"],
          raw_keys["ici_expert_parallelism"],
          raw_keys["ici_autoregressive_parallelism"],
      ]
      dcn_parallelism = [
          raw_keys["dcn_pipeline_parallelism"],
          raw_keys["dcn_data_parallelism"],
          raw_keys["dcn_fsdp_parallelism"],
          raw_keys["dcn_fsdp_transpose_parallelism"],
          raw_keys["dcn_sequence_parallelism"],
          raw_keys["dcn_tensor_parallelism"],
          raw_keys["dcn_expert_parallelism"],
          raw_keys["dcn_autoregressive_parallelism"],
      ]
      mesh_axes = ["stage", "data", "fsdp", "fsdp_transpose", "sequence", "tensor", "expert", "autoregressive"]
      data_sharding = [["stage", "data", "fsdp", "fsdp_transpose", "sequence", "tensor", "expert", "autoregressive"]]

      raw_keys["ici_parallelism"] = ici_parallelism
      raw_keys["dcn_parallelism"] = dcn_parallelism
      raw_keys["mesh_axes"] = mesh_axes
      raw_keys["data_sharding"] = data_sharding
      return raw_keys

    raw_keys["using_pipeline_parallelism"] = True
    raw_keys["logical_axis_rules"] = modify_activation_embed_and_logits_batch(raw_keys["logical_axis_rules"])
    raw_keys = pipeline_first_axis(raw_keys)
    num_stages = int(raw_keys["ici_pipeline_parallelism"] * raw_keys["dcn_pipeline_parallelism"])
    if raw_keys["num_pipeline_repeats"] == -1:
      num_pipeline_repeats, remainder = divmod(
          raw_keys["num_decoder_layers"], num_stages * raw_keys["num_layers_per_pipeline_stage"]
      )
      assert (
          not remainder
      ), f"The number of layers per stage ({raw_keys['num_layers_per_pipeline_stage']}) times the number of stages ({num_stages}) must divide the number of decoder layers ({raw_keys['num_decoder_layers']}) "
      raw_keys["num_pipeline_repeats"] = num_pipeline_repeats
    assert (
        num_stages * raw_keys["num_pipeline_repeats"] * raw_keys["num_layers_per_pipeline_stage"]
        == raw_keys["num_decoder_layers"]
    ), f"The product of pipeline stages ({num_stages}), repeats ({raw_keys['num_pipeline_repeats']}), and layers per stage ({raw_keys['num_layers_per_pipeline_stage']}) must be equal to the number of layers ({raw_keys['num_decoder_layers']})"
    if raw_keys["num_pipeline_microbatches"] == -1:
      if raw_keys["pipeline_delay_activation_forwarding"]:
        raw_keys["num_pipeline_microbatches"] = 2 * num_stages
      else:
        raw_keys["num_pipeline_microbatches"] = num_stages
    assert (
        raw_keys["num_pipeline_microbatches"] % num_stages == 0
    ), f"The number of microbatches ({raw_keys['num_pipeline_microbatches']}) must be divisible by the number of stages ({num_stages})"
    assert (
        raw_keys["micro_batch_size_to_train_on"] % raw_keys["num_pipeline_microbatches"] == 0
    ), f"The batch size ({raw_keys['micro_batch_size_to_train_on']}) must be divisible by the number of microbatches ({raw_keys['num_pipeline_microbatches']})"
    if raw_keys["pipeline_delay_activation_forwarding"]:
      assert (
          raw_keys["num_pipeline_microbatches"] >= 2 * num_stages
      ), f"Delayed activation forwarding requires at least 2 * num_stages microbatches, but {num_stages} stages are used with {raw_keys['num_pipeline_microbatches']} microbatches"
  else:
    raw_keys["using_pipeline_parallelism"] = False
  return raw_keys


def validate_megablox_parallelism(raw_keys):
  if raw_keys["megablox"] and (
      using_sequence_parallelism(raw_keys) or using_pipeline_parallelism(raw_keys) or using_expert_parallelism(raw_keys)
  ):
    raise ValueError("Currently we only support Megablox with data and tensor parallelism.")
  tensor_parallelism = raw_keys["ici_tensor_parallelism"] * raw_keys["dcn_tensor_parallelism"]
  if raw_keys["megablox"] and using_tensor_parallelism(raw_keys) and (raw_keys["emb_dim"] % tensor_parallelism):
    raise ValueError(
        f"The embedding dimension {raw_keys['emb_dim']} is not divisible by tensor parallelism setting {tensor_parallelism}."
    )


def create_new_logical_axis_rules(old_logical_axis_rules, new_logical_axis_rules):
  new_logical_axis = set()
  replacements = []
  for logical_axis, mesh_axes in new_logical_axis_rules:
    logical_axis_exists = any(rule for rule in old_logical_axis_rules if rule[0] == logical_axis)
    if not logical_axis_exists:
      continue
    replacements.append((logical_axis, mesh_axes))
    new_logical_axis.add(logical_axis)
  old_logical_rules_filtered = [
      (old_logical_axis, _lists_to_tuples(old_mesh_axes))
      for old_logical_axis, old_mesh_axes in old_logical_axis_rules
      if old_logical_axis not in new_logical_axis
  ]
  return old_logical_rules_filtered + replacements


def update_model_keys(raw_keys, model_keys, key):
  """Update `key` value in `raw_keys` from the value in `model_keys`."""
  assert key in model_keys and key in raw_keys
  if key == "logical_axis_rules":
    raw_keys[key] = create_new_logical_axis_rules(
        old_logical_axis_rules=raw_keys[key], new_logical_axis_rules=model_keys[key]
    )
    return
  raw_keys[key] = model_keys[key]


def validate_and_update_keys(raw_keys, model_keys, config_name: str):
  """Validate and update model specific config keys"""
  max_logging.log("Updating following parameters in config\n")

  for k in model_keys:
    max_logging.log(f"{k}: {model_keys[k]}")
    if k not in raw_keys:
      raise ValueError(f"Key {k} does not exist in config {config_name}.")
    elif not isinstance(raw_keys[k], type(model_keys[k])):
      raise ValueError(f"Type of key:{k} does not match with {type(model_keys[k])}")
    else:
      update_model_keys(raw_keys, model_keys, k)
  return raw_keys


def get_individual_scales(scale):
  """Choose appropriate scales for individual dimensions based on global scale
  We choose to rotate between doubling:
    num_head and mlp_dim
    embed_dim
    num_layers
  Any one of these steps is not a perfect doubling, although going through a cycle
  of three is a near perfect 8x scaling except for the linear -> softmax -> output step"""

  log_2_scale = math.floor((math.log2(scale)))
  if 2**log_2_scale != scale:
    raise ValueError(
        "Global parameter scale should be a power of 2. If you want finer grained control of the model sizes "
        "then you can explicitly set base_embed_dim, base_num_heads, base_mlp_dim, base_num_decoder_layers and/or head_dim."
    )
  base_scale, rem = divmod(log_2_scale, 3)
  num_head_scale = base_scale + int(rem > 0)
  mlp_dim_scale = num_head_scale
  emb_scale = base_scale + int(rem > 1)
  layer_scale = base_scale
  return emb_scale, num_head_scale, mlp_dim_scale, layer_scale


def calculate_global_batch_sizes(
    per_device_batch_size, expansion_factor_real_data, num_devices, gradient_accumulation_steps
):
  """Calculates target global batch size from target devices and per_device_batch"""
  if per_device_batch_size < 1.0:
    # For per_device_batch_size<1, we load the data as if per_device_batch_size=1
    if expansion_factor_real_data != -1:
      micro_batch_size_to_load = num_devices * expansion_factor_real_data
    else:
      micro_batch_size_to_load = num_devices
  else:
    if expansion_factor_real_data != -1:
      micro_batch_size_to_load = int(num_devices * per_device_batch_size * expansion_factor_real_data)
    else:
      micro_batch_size_to_load = int(num_devices * per_device_batch_size)

  micro_batch_size_to_train_on = int(num_devices * per_device_batch_size)
  global_batch_size_to_load = int(micro_batch_size_to_load * gradient_accumulation_steps)
  global_batch_size_to_train_on = int(micro_batch_size_to_train_on * gradient_accumulation_steps)
  return global_batch_size_to_load, global_batch_size_to_train_on, micro_batch_size_to_train_on


def get_num_target_devices(raw_keys):
  # In AOT case compile_topology is set (e.g. is not the empty string), and we determine the
  # number of devices from the compile_topology. In non-AOT settings we simply can use jax.devices().
  if raw_keys.get("compile_topology"):
    compile_topology = accelerator_to_spec_map.get_system_characteristics(raw_keys["compile_topology"])
    devices_per_slice = compile_topology.devices_per_slice
    return int(devices_per_slice * raw_keys["compile_topology_num_slices"])
  else:
    return len(jax.devices())


def get_quantization_local_shard_count(raw_keys):
  if raw_keys["quantization_local_shard_count"] == -1:
    return raw_keys["num_slices"]
  else:
    return raw_keys["quantization_local_shard_count"]


def using_pipeline_parallelism(raw_keys) -> bool:
  return int(raw_keys["ici_pipeline_parallelism"]) > 1 or int(raw_keys["dcn_pipeline_parallelism"]) > 1


def using_tensor_parallelism(raw_keys) -> bool:
  return int(raw_keys["ici_tensor_parallelism"]) > 1 or int(raw_keys["dcn_tensor_parallelism"]) > 1


def using_sequence_parallelism(raw_keys) -> bool:
  return int(raw_keys["ici_sequence_parallelism"]) > 1 or int(raw_keys["dcn_sequence_parallelism"]) > 1


def using_expert_parallelism(raw_keys) -> bool:
  return int(raw_keys["ici_expert_parallelism"]) > 1 or int(raw_keys["dcn_expert_parallelism"]) > 1


class HyperParameters:  # pylint: disable=missing-class-docstring

  def __init__(self):
    pass

  def __getattr__(self, attr):
    if attr not in _config.keys:
      raise ValueError(f"Requested key {attr}, not in config")
    return _config.keys[attr]

  def __setattr__(self, attr, value):
    raise ValueError

  def get_keys(self):
    return _config.keys


def initialize(argv, **kwargs):
  global _config, config
  _config = _HyperParameters(argv, **kwargs)
  config = HyperParameters()


if __name__ == "__main__":
  initialize(sys.argv)
  print(config.steps)
  r = range(config.steps)
