# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

import torch
import torch._dynamo.config
import torch._inductor.config
import torch.nn as nn

from torch.distributed.device_mesh import DeviceMesh
from torch.distributed.elastic.multiprocessing.errors import record
from torch.distributed.elastic.utils.distributed import get_free_port

from torchchat.distributed import launch_distributed, ParallelDims, parallelize_llama

from torchchat.model import Model, ModelArgs, ModelType

from torchchat.model_config.model_config import resolve_model_config
from torchchat.utils.build_utils import (
    device_sync,
    is_cpu_device,
    is_cuda_or_cpu_device,
    name_to_dtype,
)
from torchchat.utils.measure_time import measure_time
from torchchat.utils.quantize import quantize_model

from torchtune.models.convert_weights import meta_to_tune

from torchtune.models.llama3_1._position_embeddings import Llama3ScaledRoPE

from torchtune.models.llama3_2_vision._convert_weights import llama3_vision_meta_to_tune

from torchtune.training import set_default_dtype


@dataclass
class BuilderArgs:
    checkpoint_path: Optional[Union[Path, str]] = None
    checkpoint_dir: Optional[Union[Path, str]] = None
    dcp_dir: Optional[Union[Path, str]] = None
    params_path: Optional[Union[Path, str]] = None
    params_table: Optional[str] = None
    gguf_path: Optional[Union[Path, str]] = None
    gguf_kwargs: Optional[Dict[str, Any]] = None
    dso_path: Optional[Union[Path, str]] = None
    aoti_package_path: Optional[Union[Path, str]] = None
    pte_path: Optional[Union[Path, str]] = None
    device: Optional[str] = None
    precision: torch.dtype = torch.float32
    setup_caches: bool = False
    distributed: bool = False
    pp: int = 1
    tp: int = 1
    chpt_from: str = "hf"
    is_chat_model: bool = False
    prefill_possible: bool = False
    dynamic_shapes: bool = False
    max_seq_length: Optional[int] = None

    def __post_init__(self):
        if self.device is None:
            self.device = "cuda" if torch.cuda.is_available() else "cpu"

        if not (
            (self.checkpoint_path and self.checkpoint_path.is_file())
            or (self.checkpoint_dir and self.checkpoint_dir.is_dir())
            or (self.gguf_path and self.gguf_path.is_file())
            or (self.dso_path and Path(self.dso_path).is_file())
            or (self.aoti_package_path and Path(self.aoti_package_path).is_file())
            or (self.pte_path and Path(self.pte_path).is_file())
        ):
            raise RuntimeError(
                "need to specified a valid checkpoint path, checkpoint dir, gguf path, DSO path, or PTE path"
            )

        if self.aoti_package_path and self.pte_path:
            raise RuntimeError(
                "specify either AOTI Package path or PTE path, but not more than one"
            )

        if self.dso_path or self.pte_path or self.aoti_package_path:
            ignored_params = [
                (self.checkpoint_path, "checkpoint path"),
                (self.checkpoint_dir, "checkpoint dir"),
                (self.gguf_path, "GGUF path"),
            ]
            for param, param_msg in ignored_params:
                if param:
                    print(
                        f"Warning: {param_msg} ignored because an exported DSO or PTE path was specified"
                    )
        else:
            self.prefill_possible = True

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "BuilderArgs":
        # Handle disabled checkpoint_dir option
        checkpoint_dir = None
        if hasattr(args, "checkpoint_dir"):
            checkpoint_dir = args.checkpoint_dir
        if hasattr(args, "dcp_dir"):
            dcp_dir = args.dcp_dir

        checkpoint_path = args.checkpoint_path
        params_table = args.params_table
        if args.model:  # Using a named, well-known model
            model_config = resolve_model_config(args.model)

            checkpoint_path = (
                Path(args.model_directory)
                / model_config.name
                / model_config.checkpoint_file
            )
            # The transformers config is keyed on the last section
            # of the name/path.
            params_table = (
                model_config.transformer_params_key or model_config.name.split("/")[-1]
            )

        dso_path = getattr(args, "dso_path", None)
        pte_path = getattr(args, "pte_path", None)
        aoti_package_path = getattr(args, "aoti_package_path", None)

        is_chat_model = False
        if args.is_chat_model:
            is_chat_model = True
        else:
            for path in [
                checkpoint_path,
                checkpoint_dir,
                dso_path,
                pte_path,
                aoti_package_path,
                args.gguf_path,
            ]:
                if path is not None:
                    path = str(path)
                    if path.endswith("/"):
                        path = path[:-1]
                    if os.path.isfile(path):
                        path = os.path.dirname(path)

                    path_basename = os.path.basename(path).lower()
                    if "chat" in path_basename or "instruct" in path_basename:
                        is_chat_model = True

        output_pte_path = getattr(args, "output_pte_path", None)
        output_aoti_package_path = getattr(args, "output_aoti_package_path", None)
        output_dso_path = getattr(args, "output_dso_path", None)
        if output_pte_path and args.dtype.startswith("fast"):
            if args.dtype == "fast":
                # As per Kimish, float32 should be faster on ET XNNPACK
                # (because fp16 is implemented as upcast to fp32 for several
                # operators, and in particular a8w4dq and ET's sdpa+kv)
                dtype = torch.float32
            else:
                dtype = torch.float16
        else:
            dtype = name_to_dtype(args.dtype, args.device)
        # distributed args
        distributed = getattr(args, "distributed", False)
        pp = getattr(args, "pp", 1)
        tp = getattr(args, "tp", 1)
        chpt_from = getattr(args, "chpt_from", "hf")
        return cls(
            checkpoint_dir=checkpoint_dir,
            checkpoint_path=checkpoint_path,
            dcp_dir=dcp_dir,
            params_path=args.params_path,
            params_table=params_table,
            gguf_path=args.gguf_path,
            gguf_kwargs=None,
            dso_path=dso_path,
            aoti_package_path=aoti_package_path,
            pte_path=pte_path,
            device=args.device,
            precision=dtype,
            setup_caches=(
                output_dso_path or output_pte_path or output_aoti_package_path
            ),
            distributed=distributed,
            pp=pp,
            tp=tp,
            chpt_from=chpt_from,
            is_chat_model=is_chat_model,
            dynamic_shapes=getattr(args, "dynamic_shapes", False),
            max_seq_length=getattr(args, "max_seq_length", None),
        )

    @classmethod
    def from_speculative_args(cls, args: argparse.Namespace) -> "BuilderArgs":
        speculative_builder_args = BuilderArgs.from_args(args)
        # let's limit multi-checkpoint to checker
        speculative_builder_args.checkpoint_dir = None
        speculative_builder_args.checkpoint_path = args.draft_checkpoint_path
        speculative_builder_args.gguf_path = None
        speculative_builder_args.dso_path = None
        speculative_builder_args.aoti_package_path = None
        speculative_builder_args.pte_path = None
        return speculative_builder_args


@dataclass
class TokenizerArgs:
    tokenizer_path: Optional[Union[Path, str]] = None
    is_sentencepiece: bool = False
    is_tiktoken: bool = False
    is_hf_tokenizer: bool = False
    t: Optional[Any] = None

    def __post_init__(self):
        try:
            from tokenizer.tiktoken import Tokenizer as TiktokenTokenizer

            self.t = TiktokenTokenizer(model_path=str(self.tokenizer_path))
            self.is_tiktoken = True
            self.is_sentencepiece = False
            self.is_hf_tokenizer = False
            return
        except:
            pass

        try:
            from sentencepiece import SentencePieceProcessor

            self.t = SentencePieceProcessor(model_file=str(self.tokenizer_path))
            self.is_tiktoken = False
            self.is_sentencepiece = True
            self.is_hf_tokenizer = False
            return
        except:
            pass

        try:
            from tokenizer.hf_tokenizer import HFTokenizer

            self.t = HFTokenizer(str(self.tokenizer_path))
            self.is_tiktoken = False
            self.is_sentencepiece = False
            self.is_hf_tokenizer = True
            return
        except:
            pass

        self.is_tiktoken = False
        self.is_sentencepiece = False
        self.is_hf_tokenizer = False
        self.t = None
        return

    def validate_model(
        self,
        model: Optional[Model],
        model_description: str = "model",
    ) -> None:
        if model is None:
            return

        if sum([self.is_tiktoken, self.is_hf_tokenizer, self.is_sentencepiece]) != 1:
            raise RuntimeError(f"no tokenizer was found at {self.tokenizer_path}")

        is_tiktoken = self.is_tiktoken
        is_sentencepiece = self.is_sentencepiece
        is_hf_tokenizer = self.is_hf_tokenizer
        use_tiktoken = model.config.use_tiktoken
        use_hf_tokenizer = model.config.use_hf_tokenizer
        use_sentencepiece = not (use_tiktoken or use_hf_tokenizer)

        if (
            (is_tiktoken and not use_tiktoken) or
            (is_hf_tokenizer and not use_hf_tokenizer) or
            (is_sentencepiece and not use_sentencepiece)
        ):
            raise RuntimeError(
                "model-specified tokenizer ({}) does not match provided tokenizer ({}) for {}".format(
                    tokenizer_setting_to_name(use_tiktoken, use_hf_tokenizer),
                    tokenizer_setting_to_name(is_tiktoken, is_hf_tokenizer),
                    model_description,
                )
            )

        return

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "TokenizerArgs":
        """
        Create a TokenizerArgs object from command line arguments.
        Specifically, `tokenizer_path` is resolved with precedence:
          * From Explicitly provided tokenizer_path
          * Resolve via model_config identified by args.model
          * Look in the directory of args.checkpoint_path for tokenizer.model
          * Look in the directory of args.checkpoint_dir for tokenizer.model

        Args:
            args (argparse.Namespace): The command line arguments.

        Returns:
            TokenizerArgs: A TokenizerArgs object.
        """
        if args.tokenizer_path:
            tokenizer_path = args.tokenizer_path
        elif args.model:  # Using a named, well-known model
            model_config = resolve_model_config(args.model)
            tokenizer_path = (
                Path(args.model_directory)
                / model_config.name
                / model_config.tokenizer_file
            )
        elif args.checkpoint_path:
            tokenizer_path = args.checkpoint_path.parent / "tokenizer.model"
        elif hasattr(args, "checkpoint_dir") and args.checkpoint_dir:
            tokenizer_path = args.checkpoint_dir / "tokenizer.model"
        else:
            raise RuntimeError("cannot find tokenizer model")

        if not tokenizer_path.is_file():
            raise RuntimeError(
                f"did not find tokenizer at {os.path.abspath(tokenizer_path)}"
            )

        return cls(tokenizer_path=tokenizer_path)


def _initialize_tokenizer(tokenizer_args: TokenizerArgs):
    return tokenizer_args.t


torch._inductor.config.coordinate_descent_tuning = True
torch._inductor.config.triton.unique_kernel_names = True
torch._inductor.config.fx_graph_cache = True  # Experimental feature to reduce compilation times, will be on by default in future


# support running without installing as a package
wd = Path(__file__).parent.parent.resolve()
sys.path.append(str(wd))


# TODO: remove these once ET supports _weight_int4pack_mm
def _set_gguf_kwargs(builder_args: BuilderArgs, is_et: bool, context: str) -> None:
    assert context in ["export", "generate"]
    assert builder_args.gguf_kwargs is None

    if builder_args.gguf_path is None:
        print("No gguf_path provided, so ignoring set_gguf_kwargs.")
        return

    builder_args.gguf_kwargs = {}
    if is_et:
        builder_args.gguf_kwargs["load_as_quantized"] = False


def _unset_gguf_kwargs(builder_args: BuilderArgs) -> None:
    builder_args.gguf_kwargs = None


def _init_model_on_meta_device(builder_args: BuilderArgs) -> Model:
    with torch.device("meta"):
        if builder_args.params_path:
            return Model.from_params(builder_args.params_path)
        elif builder_args.params_table:
            return Model.from_table(builder_args.params_table)
        else:
            return Model.from_name(builder_args.checkpoint_path.parent.name)


def _load_model_gguf(builder_args: BuilderArgs) -> Model:
    assert builder_args.gguf_path
    if builder_args.gguf_kwargs is None:
        kwargs = {}
    else:
        kwargs = builder_args.gguf_kwargs
    model = Model.from_gguf(builder_args.gguf_path, **kwargs)
    return model


def _load_checkpoint(builder_args: BuilderArgs):
    if builder_args.params_table and builder_args.params_table.endswith("Tune"):
        print("Loading Tune checkpoint")
        meta_checkpoint = torch.load(
            str(builder_args.checkpoint_path), mmap=True, weights_only=True
        )
        checkpoint = meta_to_tune(meta_checkpoint)
    elif builder_args.checkpoint_dir is not None:
        # Load multiple checkpoint; ignore the single path.
        builder_args.checkpoint_path = None
        cps = []
        for i in range(4):
            cp_name = f"consolidated.{i}.pth"
            print(f"Loading {cp_name}")
            cps.append(
                torch.load(
                    os.path.join(builder_args.checkpoint_dir, cp_name),
                    map_location=builder_args.device,
                    mmap=True,
                )
            )
        checkpoint = {}
        for key in cps[0].keys():
            if not torch.allclose(cps[0][key], cps[1][key]):
                values = (cps[0][key], cps[1][key], cps[2][key], cps[3][key])
                if key.endswith("wo.weight") or key.endswith("w2.weight"):
                    checkpoint[key] = torch.cat(values, dim=1)
                else:
                    checkpoint[key] = torch.cat(values, dim=0)
            else:
                checkpoint[key] = cps[0][key]
    else:
        checkpoint = torch.load(
            str(builder_args.checkpoint_path),
            map_location=builder_args.device,
            mmap=True,
            weights_only=True,
        )
    return checkpoint


def _load_model_default(builder_args: BuilderArgs) -> Model:
    assert not builder_args.gguf_path

    model: Model = _init_model_on_meta_device(builder_args)

    # Load checkpoint from filesystem
    checkpoint = _load_checkpoint(builder_args)

    if "model" in checkpoint and "stories" in str(builder_args.checkpoint_path):
        checkpoint = checkpoint["model"]

    if model.config.model_type == ModelType.Flamingo:
        # TODO: Refactor this. For now, overwrite the model with model loaded from params_path
        with set_default_dtype(builder_args.precision), torch.device(
            builder_args.device
        ):
            # It doubles the model size the memory, with redundancies of the initialized weights.
            # model = Model.from_params(builder_args.params_path)

            # Buffers in rotary embedding are not included in the checkpoint.
            # Instead, they are calculated in initialization. Since buffers on meta device
            # does not host any actual values, need to reinitialize them in the actual
            # device. Only do those buffer initialization, without initializing the entire
            # model.
            decoder_config = model.config.transformer_args["decoder"]
            head_dim = decoder_config["embed_dim"] // decoder_config["num_heads"]
            max_seq_len = decoder_config["max_seq_len"]
            rope_base = decoder_config["rope_base"]
            for submodule in model.modules():
                if isinstance(submodule, Llama3ScaledRoPE):
                    submodule.__init__(head_dim, max_seq_len, rope_base)
        state_dict = llama3_vision_meta_to_tune(checkpoint)
        model.model.load_state_dict(state_dict, assign=True, strict=False)
    else:
        checkpoint = {"model." + k: v for k, v in checkpoint.items()}
        model.load_state_dict(checkpoint, assign=True, strict=True)

    return model


def _maybe_init_distributed(
    builder_args: BuilderArgs,
) -> Tuple[Optional[DeviceMesh], Optional[ParallelDims]]:
    """
    Initialize distributed related setups if the user specified
    using distributed inference. If not, this is a no-op.

    Args:
        builder_args (:class:`BuilderArgs`):
            Command args for model building.
    Returns:
        Tuple[Optional[DeviceMesh], Optional[ParallelDims]]:
            - The first element is an optional DeviceMesh object,
            which which describes the mesh topology of devices for the DTensor.
            - The second element is an optional ParallelDims object,
            which represents the parallel dimensions configuration.
    """
    if not builder_args.use_distributed:
        return None, None
    dist_config = "llama3_8B.toml"  # TODO - integrate with chat cmd line

    world_mesh, parallel_dims = launch_distributed(dist_config)

    assert (
        world_mesh is not None and parallel_dims is not None
    ), f"failed to launch distributed using {dist_config}"

    return world_mesh, parallel_dims


def _maybe_parallelize_model(
    model: nn.Module,
    builder_args: BuilderArgs,
    world_mesh: DeviceMesh,
    parallel_dims: ParallelDims,
) -> nn.Module:
    """
    We parallelize the module and load the distributed checkpoint to the model
    if the user specifies using distributed inference. If not, this is a no-op.

    Args:
        model (:class:`nn.Module`):
            Module to be parallelized.
        builder_args (:class:`BuilderArgs`):
            Command args for model building.
        world_mesh (:class:`DeviceMesh`):
            Object which describes the mesh topology
            of devices for the DTensor.
        parallel_dims (:class:`ParallelDims`):
            Object which represents the parallel dimensions configuration.
    Returns:
        A :class:`nn.Module` object which is parallelized and checkpoint loaded
        if the user specifies using distributed inference.
    """
    if world_mesh is None:
        return model
    assert parallel_dims is not None
    print("Applying model parallel to model ...")
    parallelize_llama(model, world_mesh, parallel_dims)
    return load_checkpoints_to_model(model, builder_args, world_mesh)


def _load_model(builder_args: BuilderArgs) -> Model:
    # world_mesh, parallel_dims = _maybe_init_distributed(builder_args)
    if builder_args.gguf_path:
        model = _load_model_gguf(builder_args)
    # elif builder_args.use_distributed:
    #    model = _init_model_on_meta_device(builder_args)
    else:
        model = _load_model_default(builder_args)
    # model = _maybe_parallelize_model(model, builder_args, world_mesh, parallel_dims)

    model = model.to(device=builder_args.device, dtype=builder_args.precision)
    return model.eval()


def _initialize_model(
    builder_args: BuilderArgs,
    quantize,
    tokenizer=None,
    max_seq_length=None,
    support_tensor_subclass: bool = True,
) -> Model:
    print("Loading model...")
    if builder_args.gguf_path and (
        builder_args.dso_path or builder_args.pte_path or builder_args.aoti_package_path
    ):
        print("Setting gguf_kwargs for generate.")
        is_dso = builder_args.dso_path is not None
        is_aoti_package = builder_args.aoti_package_path is not None
        is_pte = builder_args.pte_path is not None
        assert not (is_dso and is_aoti_package and is_pte)
        assert builder_args.gguf_kwargs is None
        # TODO: make GGUF load independent of backend
        # currently not working because AVX int_mm broken
        #   (no unpack available)
        _set_gguf_kwargs(builder_args, is_et=is_pte, context="generate")

    if builder_args.dso_path:
        if not is_cuda_or_cpu_device(builder_args.device):
            print(
                f"Cannot load specified DSO to {builder_args.device}. Attempting to load model to CPU instead"
            )
            builder_args.device = "cpu"

        # assert (
        #     quantize is None or quantize == "{ }"
        # ), "quantize not valid for exported DSO model. Specify quantization during export."

        with measure_time("Time to load model: {time:.02f} seconds"):
            model = _load_model(builder_args)
            device_sync(device=builder_args.device)

        try:
            # Replace model forward with the AOT-compiled forward
            # This is a hacky way to quickly demo AOTI's capability.
            # model is still a Python object, and any mutation to its
            # attributes will NOT be seen on by AOTI-compiled forward
            # function, e.g. calling model.setup_cache will NOT touch
            # AOTI compiled and maintained model buffers such as kv_cache.
            model.forward = torch._export.aot_load(
                str(builder_args.dso_path.absolute()), builder_args.device
            )
        except:
            raise RuntimeError(f"Failed to load AOTI compiled {builder_args.dso_path}")

    elif builder_args.aoti_package_path:
        if not is_cuda_or_cpu_device(builder_args.device):
            print(
                f"Cannot load specified PT2 to {builder_args.device}. Attempting to load model to CPU instead"
            )
            builder_args.device = "cpu"

        # assert (
        #     quantize is None or quantize == "{ }"
        # ), "quantize not valid for exported PT2 model. Specify quantization during export."

        with measure_time("Time to load model: {time:.02f} seconds"):
            model = _load_model(builder_args)
            device_sync(device=builder_args.device)

        try:
            # Replace model forward with the AOT-compiled forward
            # This is a hacky way to quickly demo AOTI's capability.
            # model is still a Python object, and any mutation to its
            # attributes will NOT be seen on by AOTI-compiled forward
            # function, e.g. calling model.setup_cache will NOT touch
            # AOTI compiled and maintained model buffers such as kv_cache.
            from torch._inductor.package import load_package

            aoti_compiled_model = load_package(
                str(builder_args.aoti_package_path.absolute())
            )
            model.forward = aoti_compiled_model
            metadata = aoti_compiled_model.get_metadata()
            builder_args.device = metadata["AOTI_DEVICE_KEY"]
        except:
            raise RuntimeError(
                f"Failed to load AOTI compiled {builder_args.aoti_package_path}"
            )

    elif builder_args.pte_path:
        if not is_cpu_device(builder_args.device):
            print(
                f"Cannot load specified PTE to {builder_args.device}. Attempting to load model to CPU instead"
            )
            builder_args.device = "cpu"

        # Resolve ModelArgs for constructing the PTEModel
        # If a manual params_path is provided, use that
        if builder_args.params_path:
            config: ModelArgs = ModelArgs.from_params(builder_args.params_path)
        else:
            # TODO: Instead of loading the whole model, refactor to call a
            # helper that generate just model.config
            with measure_time("Time to load model: {time:.02f} seconds"):
                model = _load_model(builder_args)
                device_sync(device=builder_args.device)
                config = model.config

        try:
            from torchchat.model import PTEModel

            model = PTEModel(config, builder_args.pte_path)
        except Exception:
            raise RuntimeError(f"Failed to load ET compiled {builder_args.pte_path}")
    else:
        with measure_time("Time to load model: {time:.02f} seconds"):
            model = _load_model(builder_args)
            device_sync(device=builder_args.device)

        if quantize:
            print(f"Quantizing the model with: {quantize}")
            with measure_time("Time to quantize model: {time:.02f} seconds"):
                quantize_model(
                    model,
                    builder_args.device,
                    quantize,
                    tokenizer,
                    support_tensor_subclass,
                )
                device_sync(device=builder_args.device)

        if builder_args.setup_caches:
            with torch.device(builder_args.device):
                model.setup_caches(
                    max_batch_size=1,
                    max_seq_length=max_seq_length
                    or model.text_transformer_args.max_seq_length,
                )

        model.to(dtype=builder_args.precision)

    print("-----------------------------------------------------------")
    return model


def tokenizer_setting_to_name(tiktoken: bool, tokenizers: bool) -> str:
    if tiktoken:
        return "TikToken"
    if tokenizers:
        return "Tokenizers"
    return "SentencePiece"