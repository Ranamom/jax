# Copyright 2023 The JAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Contains registrations for pallas_call on TPU."""

from __future__ import annotations

import os
import tempfile
from typing import Any
import warnings

import jax
from jax import core as jax_core
from jax import dtypes
from jax._src import config
from jax._src import core as jax_src_core
from jax._src import sharding_impls
from jax._src.interpreters import mlir
from jax._src.lib.mlir import ir
from jax._src.pallas import core
from jax._src.pallas.mosaic import lowering
from jax._src.pallas.mosaic import verification
from jax.experimental import mosaic
from jax.experimental.mosaic.dialects import tpu
from jax.experimental.pallas import tpu as pltpu

def _maybe_cast_to_int(x: jax.Array | jax_core.ShapedArray):
  """Casts boolean values to integers.

  We perform this cast because Mosaic does not directly support bool values
  for Memrefs. Instead, we load bools as integers and cast them to bools
  after loading from a memref inside of the kernel.
  """
  if isinstance(x, jax.Array):
    if dtypes.issubdtype(x.dtype, jax.numpy.bool_):
      return x.astype(lowering.BOOL_MEMREF_TYPE)
    return x
  else:
    if dtypes.issubdtype(x.dtype, jax.numpy.bool_):
      return jax_core.ShapedArray(x.shape, lowering.BOOL_MEMREF_TYPE)
    return x

_DUMP_PROMELA_TO = config.string_flag(
    "jax_pallas_dump_promela_to",
    default=os.getenv("JAX_PALLAS_DUMP_PROMELA_TO", ""),
    help=(
        "If set, dumps a Promela model of the kernel to the specified"
        " directory. The model can verify that the kernel is free of data"
        " races, deadlocks, etc."
    ),
)


def pallas_call_tpu_lowering_rule(
    ctx: mlir.LoweringRuleContext,
    *in_nodes,
    jaxpr: jax_core.Jaxpr,
    name_and_src_info: core.NameAndSrcInfo,
    grid_mapping: core.GridMapping,
    input_output_aliases: tuple[tuple[int, int], ...],
    debug: bool,
    interpret: bool,
    compiler_params: dict[str, Any],
    cost_estimate: core.CostEstimate | None,
):
  """Lowers a pallas_call to a Mosaic TPU custom call."""
  del interpret
  if debug:
    print(f"\nThe kernel jaxpr for pallas_call {name_and_src_info}:")
    print(jaxpr)
  if "mosaic" in compiler_params:
    mosaic_params = compiler_params["mosaic"]
  else:
    mosaic_params = {}

  if "cost_estimate" in mosaic_params:
    # TODO(amagni): Remove this branch after October 22th 2024.
    if cost_estimate is not None:
      raise ValueError(
          "Passing cost estimate via both compiler_params=dict(mosaic=...) and"
          " pallas_call(..., cost_estimate=...) is not supported."
      )

    warnings.warn(
        "Passing cost estimate via compiler_params=dict(cost_estimate=...) is"
        " deprecated. Use pallas_call(..., cost_estimate=...) instead.",
        DeprecationWarning,
    )
    cost_estimate = mosaic_params["cost_estimate"]

  mesh = None
  axis_context = ctx.module_context.axis_context
  if axis_context is not None:
    if isinstance(axis_context, sharding_impls.SPMDAxisContext):
      mesh = axis_context.mesh
  mlir_ctx = ir.Context()
  mlir_ctx.append_dialect_registry(mlir.upstream_dialects)
  mlir_ctx.load_all_available_dialects()
  tpu.register_dialect(mlir_ctx)
  def lower_module(for_verification: bool):
    if for_verification:
      mlir_ctx.allow_unregistered_dialects = True
    with mlir_ctx, ir.Location.unknown(mlir_ctx):
      dimension_semantics = mosaic_params.get("dimension_semantics", None)
      return lowering.lower_jaxpr_to_module(
          ctx, mlir_ctx, grid_mapping, jaxpr,
          dimension_semantics=dimension_semantics, mesh=mesh,
          for_verification=for_verification,
          name_and_src_info=name_and_src_info)
  mosaic_module, extra_args = lower_module(for_verification=False)
  if debug:
    print(f"\nThe Mosaic module for pallas_call {name_and_src_info}:")
    print(mosaic_module)
  num_extra_args = len(extra_args)
  num_dyn_bounds = grid_mapping.num_dynamic_grid_bounds
  input_output_aliases = tuple(
      (a[0] + num_dyn_bounds + num_extra_args, a[1])
      for a in input_output_aliases
  )
  out_avals = [jax_core.ShapedArray(bm.array_shape_dtype.shape,
                                    bm.array_shape_dtype.dtype)
               for bm in grid_mapping.block_mappings_output]

  if promela_dump_path := _DUMP_PROMELA_TO.value:
    num_devices = 1 if mesh is None else mesh.devices.size
    num_cores = (
        jax.devices()[0].num_cores
        if mesh is None
        else mesh.devices[0].num_cores
    )
    verification_module, _ = lower_module(for_verification=True)
    model = verification.export_promela_model(
        verification_module, num_devices, num_cores
    )
    if promela_dump_path == "stdout":
      print(f"The Promela model for pallas_call {name_and_src_info}:")
      print(model)
    else:
      if promela_dump_path == "sponge":
        promela_dump_path = os.getenv("TEST_UNDECLARED_OUTPUTS_DIR", "")
        if not promela_dump_path:
          raise ValueError(
              "TEST_UNDECLARED_OUTPUTS_DIR must be set when"
              " --jax_pallas_dump_promela_to=sponge"
          )
      dump_ctx = tempfile.NamedTemporaryFile(
          mode="w",
          prefix=name_and_src_info.name + "-",
          suffix=".pml",
          dir=promela_dump_path, delete=False,
      )
      with dump_ctx as f:
        f.write(model)

  # Replace in_avals to physical avals.
  # This step is required for mapping logical types to physical types.
  # (e.g. PRNG key -> uint32[2])
  physical_avals = [jax_src_core.physical_aval(aval) for aval in ctx.avals_in]
  ctx = ctx.replace(avals_in=physical_avals)

  # Booleans are loaded into the kernel as integers.
  def _maybe_cast_inputs(*args):
    args = [_maybe_cast_to_int(x) for x in args]
    return args
  kernel_in_avals = [_maybe_cast_to_int(x) for x in ctx.avals_in]  # type: ignore
  kernel_out_avals = [_maybe_cast_to_int(x) for x in out_avals]
  cast_ctx = ctx.replace(avals_out=kernel_in_avals)
  in_nodes = mlir.lower_fun(_maybe_cast_inputs)(cast_ctx, *in_nodes)

  # Dynamic grid bounds have to go at the front.
  dynamic_grid_args, args = in_nodes[:num_dyn_bounds], in_nodes[num_dyn_bounds:]
  kernel_ctx = ctx.replace(avals_in=kernel_in_avals, avals_out=kernel_out_avals)
  if cost_estimate is not None:
    mosaic_cost_estimate = pltpu.CostEstimate(
        flops=cost_estimate.flops,
        bytes_accessed=cost_estimate.bytes_accessed,
        transcendentals=cost_estimate.transcendentals,
    )
  else:
    mosaic_cost_estimate = None
  out_nodes = mosaic.lower_module_to_custom_call(
      kernel_ctx,
      *dynamic_grid_args,
      *extra_args,
      *args,
      module=mosaic_module,
      out_type=kernel_out_avals,
      backend="tpu",
      kernel_name=name_and_src_info.name,
      cost_estimate=mosaic_cost_estimate,
      vmem_limit_bytes=mosaic_params.get("vmem_limit_bytes"),
      flags=mosaic_params.get("flags"),
      allow_input_fusion=mosaic_params.get("allow_input_fusion"),
      input_output_aliases=input_output_aliases,
      serialization_format=mosaic_params.get("serialization_format", 1),
      device_type=mosaic_params.get("device_type"),
      internal_scratch_in_bytes=mosaic_params.get("internal_scratch_in_bytes"),
      collective_id=mosaic_params.get("collective_id", None),
  )
  _maybe_cast_to_bool = lambda x, aval: x.astype(
      jax.numpy.bool_) if aval.dtype == jax.numpy.bool_ else x
  def _maybe_cast_outputs(*args):
    args = [_maybe_cast_to_bool(x, aval) for x, aval in zip(args, out_avals)]
    return args
  cast_ctx = ctx.replace(avals_in=kernel_out_avals)
  return mlir.lower_fun(_maybe_cast_outputs)(cast_ctx, *out_nodes)
