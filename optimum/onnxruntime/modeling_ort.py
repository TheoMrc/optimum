#  Copyright 2022 The HuggingFace Team. All rights reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""ORTModelForXXX classes, allowing to run ONNX Models with ONNX Runtime using the same API as Transformers."""

import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, Union

import numpy as np
import torch
from transformers import (
    AutoConfig,
    AutoModel,
    AutoModelForImageClassification,
    AutoModelForMultipleChoice,
    AutoModelForQuestionAnswering,
    AutoModelForSequenceClassification,
    AutoModelForTokenClassification,
)
from transformers.file_utils import add_start_docstrings, add_start_docstrings_to_model_forward, default_cache_path
from transformers.modeling_outputs import (
    BaseModelOutput,
    ImageClassifierOutput,
    ModelOutput,
    MultipleChoiceModelOutput,
    QuestionAnsweringModelOutput,
    SequenceClassifierOutput,
    TokenClassifierOutput,
)

import onnxruntime as ort
from huggingface_hub import hf_hub_download

from ..exporters import TasksManager
from ..exporters.onnx import export
from ..modeling_base import FROM_PRETRAINED_START_DOCSTRING, OptimizedModel
from .io_binding import TypeHelper
from .utils import (
    ONNX_WEIGHTS_NAME,
    get_device_for_provider,
    get_provider_for_device,
    parse_device,
    validate_provider_availability,
)


if TYPE_CHECKING:
    from transformers import PretrainedConfig


logger = logging.getLogger(__name__)


_TOKENIZER_FOR_DOC = "AutoTokenizer"
_FEATURE_EXTRACTOR_FOR_DOC = "AutoFeatureExtractor"

ONNX_MODEL_START_DOCSTRING = r"""
    This model inherits from [~`onnxruntime.modeling_ort.ORTModel`]. Check the superclass documentation for the generic methods the
    library implements for all its model (such as downloading or saving)

    Args:
        config (`transformers.PretrainedConfig`): [PretrainedConfig](https://huggingface.co/docs/transformers/main_classes/configuration#transformers.PretrainedConfig) is the Model configuration class with all the parameters of the model.
            Initializing with a config file does not load the weights associated with the model, only the
            configuration. Check out the [`~onnxruntime.modeling_ort.ORTModel.from_pretrained`] method to load the model weights.
        model (`onnxruntime.InferenceSession`): [onnxruntime.InferenceSession](https://onnxruntime.ai/docs/api/python/api_summary.html#inferencesession) is the main class used to run a model. Check out the [`~onnxruntime.modeling_ort.ORTModel.load_model`] method for more information.
        use_io_binding (`bool`, *optional*): Whether to use IOBinding during inference to avoid memory copy between the host and devices. Defaults to `True` if the device is CUDA, otherwise defaults to `False`.
"""

ONNX_TEXT_INPUTS_DOCSTRING = r"""
    Args:
        input_ids (`torch.Tensor` of shape `({0})`):
            Indices of input sequence tokens in the vocabulary.
            Indices can be obtained using [`AutoTokenizer`](https://huggingface.co/docs/transformers/autoclass_tutorial#autotokenizer).
            See [`PreTrainedTokenizer.encode`](https://huggingface.co/docs/transformers/main_classes/tokenizer#transformers.PreTrainedTokenizerBase.encode) and
            [`PreTrainedTokenizer.__call__`](https://huggingface.co/docs/transformers/main_classes/tokenizer#transformers.PreTrainedTokenizerBase.__call__) for details.
            [What are input IDs?](https://huggingface.co/docs/transformers/glossary#input-ids)
        attention_mask (`torch.Tensor` of shape `({0})`, *optional*):
            Mask to avoid performing attention on padding token indices. Mask values selected in `[0, 1]`:
            - 1 for tokens that are **not masked**,
            - 0 for tokens that are **masked**.
            [What are attention masks?](https://huggingface.co/docs/transformers/glossary#attention-mask)
        token_type_ids (`torch.Tensor` of shape `({0})`, *optional*):
            Segment token indices to indicate first and second portions of the inputs. Indices are selected in `[0, 1]`:
            - 1 for tokens that are **sentence A**,
            - 0 for tokens that are **sentence B**.
            [What are token type IDs?](https://huggingface.co/docs/transformers/glossary#token-type-ids)
"""

ONNX_IMAGE_INPUTS_DOCSTRING = r"""
    Args:
        pixel_values (`torch.Tensor` of shape `({0})`):
            Pixel values corresponding to the images in the current batch.
            Pixel values can be obtained from encoded images using [`AutoFeatureExtractor`](https://huggingface.co/docs/transformers/autoclass_tutorial#autofeatureextractor).
"""


class ORTModel(OptimizedModel):
    """
    Base class for implementing models using ONNX Runtime.

    The ORTModel implements generic methods for interacting with the Hugging Face Hub as well as exporting vanilla
    transformers models to ONNX using `optimum.exporters.onnx` toolchain.

    Class attributes:
        - model_type (`str`, *optional*, defaults to `"onnx_model"`) -- The name of the model type to use when
        registering the ORTModel classes.
        - auto_model_class (`Type`, *optional*, defaults to `AutoModel`) -- The "AutoModel" class to represented by the
        current ORTModel class.

    Common attributes:
        - model (`ort.InferenceSession`) -- The ONNX Runtime InferenceSession that is running the model.
        - config ([`~transformers.PretrainedConfig`] -- The configuration of the model.
        - use_io_binding (`bool`, *optional*, defaults to `True`) -- Whether to use I/O bindings with **ONNX Runtime
        with the CUDAExecutionProvider**, this can significantly speedup inference depending on the task.
        - model_save_dir (`Optional[str]`, *optional*) -- The directory where the model exported to ONNX will be saved.
        By defaults, if the loaded model is local, the directory where the original model will be used. Otherwise, the
        cache directory is used.
        - latest_model_name (`str`, *optional*, defaults to `"model.onnx"` -- The name of the last ONNX model file.
        - providers (`List[str]) -- The list of execution providers available to ONNX Runtime.
    """

    model_type = "onnx_model"
    auto_model_class = AutoModel

    def __init__(
        self,
        model: ort.InferenceSession,
        config: "PretrainedConfig",
        use_io_binding: bool = True,
        model_save_dir: Optional[str] = None,
        latest_model_name: str = "model.onnx",
    ):
        self.model = model
        self.config = config
        self.use_io_binding = use_io_binding
        self.model_save_dir = model_save_dir
        self.latest_model_name = latest_model_name
        self.providers = model.get_providers()
        self._device = get_device_for_provider(self.providers[0])

        if self._device is None:
            logger.warning(
                f"ORTModel outputs will be sent to CPU as the device could not be inferred from the execution provider {self.providers[0]}."
                f" Use `ort_model.to()` to send the outputs to the wanted device."
            )

        if "TensorrtExecutionProvider" in self.providers and self.use_io_binding:
            logger.warning(
                "There is no need to do IO binding for TensorrtExecutionProvider, `use_io_binding` is set to False."
            )
            self.use_io_binding = False

        # Registers the ORTModelForXXX classes into the transformers AutoModel classes to avoid warnings when creating
        # a pipeline https://github.com/huggingface/transformers/blob/cad61b68396a1a387287a8e2e2fef78a25b79383/src/transformers/pipelines/base.py#L863
        AutoConfig.register(self.model_type, AutoConfig)
        self.auto_model_class.register(AutoConfig, self.__class__)

    # TODO: why do we make device a property since we are only access the value, and do not do any check when setting the value?
    @property
    def device(self) -> torch.device:
        """
        `torch.device`: The device on which the module is (assuming that all the module parameters are on the same
        device).
        """
        return self._device

    @device.setter
    def device(self, value: torch.device):
        self._device = value

    def to(self, device: Union[torch.device, str, int]):
        """
        Changes the ONNX Runtime provider according to the device.

        Args:
            device (`torch.device` or `str` or `int`):
                Device ordinal for CPU/GPU supports. Setting this to -1 will leverage CPU, a positive will run
                the model on the associated CUDA device id. You can pass native `torch.device` or a `str` too.

        Returns:
            `ORTModel`: the model placed on the requested device.
        """
        device, provider_options = parse_device(device)

        self.device = device
        provider = get_provider_for_device(self.device)
        validate_provider_availability(provider)  # raise error if the provider is not available

        self.model.set_providers([provider], provider_options=[provider_options])
        self.providers = self.model.get_providers()

        return self

    def forward(self, *args, **kwargs):
        raise NotImplementedError

    @staticmethod
    def load_model(
        path: Union[str, Path],
        provider: str = "CPUExecutionProvider",
        session_options: Optional[ort.SessionOptions] = None,
        provider_options: Optional[Dict[str, Any]] = None,
    ) -> ort.InferenceSession:
        """
        Loads an ONNX Inference session with a given provider. Default provider is `CPUExecutionProvider` to match the
        default behaviour in PyTorch/TensorFlow/JAX.

        Args:
            path (`Union[str, Path]`):
                Path of the ONNX model.
            provider (`str`, *optional*, defaults to `"CPUExecutionProvider"`):
                ONNX Runtime provider to use for loading the model. See https://onnxruntime.ai/docs/execution-providers/
                for possible providers.
            session_options (`Optional[onnxruntime.SessionOptions]`, *optional*):
                ONNX Runtime session options to use for loading the model.
            provider_options (`Optional[Dict[str, Any]]`, *optional*):
                Provider option dictionary corresponding to the provider used. See available options
                for each provider: https://onnxruntime.ai/docs/api/c/group___global.html .
        """
        validate_provider_availability(provider)  # raise error if the provider is not available

        providers = [provider]
        if provider == "TensorrtExecutionProvider":
            # Follow advice in https://onnxruntime.ai/docs/execution-providers/TensorRT-ExecutionProvider.html#python
            providers.append("CUDAExecutionProvider")

        # `providers` list must of be of the same length as `provider_options` list
        return ort.InferenceSession(
            path,
            providers=providers,
            sess_options=session_options,
            provider_options=None if provider_options is None else [provider_options],
        )

    def _save_pretrained(self, save_directory: Union[str, Path], file_name: str = ONNX_WEIGHTS_NAME):
        """
        Saves a model and its configuration file to a directory, so that it can be re-loaded using the
        [`~optimum.onnxruntime.modeling_ort.ORTModel.from_pretrained`] class method. It will always save the
        file under model_save_dir/latest_model_name.

        Args:
            save_directory (`Union[str, Path]`):
                Directory where to save the model file.
            file_name (`str`, *optional*, defaults to the value of `optimum.onnxruntime.utils.ONNX_WEIGHTS_NAME`):
                The filename to use when saving the model.
        """
        src_path = self.model_save_dir.joinpath(self.latest_model_name)
        dst_path = Path(save_directory).joinpath(file_name)
        shutil.copyfile(src_path, dst_path)

    @classmethod
    def _from_pretrained(
        cls,
        model_id: Union[str, Path],
        config: "PretrainedConfig",
        use_auth_token: Optional[Union[bool, str]] = None,
        revision: Optional[str] = None,
        force_download: bool = False,
        cache_dir: Optional[str] = None,
        file_name: str = ONNX_WEIGHTS_NAME,
        subfolder: str = "",
        local_files_only: bool = False,
        provider: str = "CPUExecutionProvider",
        session_options: Optional[ort.SessionOptions] = None,
        provider_options: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> "ORTModel":
        if os.path.isdir(os.path.join(model_id, subfolder)):
            model = ORTModel.load_model(
                os.path.join(model_id, subfolder, file_name),
                provider=provider,
                session_options=session_options,
                provider_options=provider_options,
            )
            kwargs["model_save_dir"] = Path(model_id).joinpath(subfolder)
            kwargs["latest_model_name"] = file_name
        else:
            model_cache_path = hf_hub_download(
                repo_id=model_id,
                filename=file_name,
                subfolder=subfolder,
                use_auth_token=use_auth_token,
                revision=revision,
                cache_dir=cache_dir,
                force_download=force_download,
                local_files_only=local_files_only,
            )
            model = ORTModel.load_model(
                model_cache_path, provider=provider, session_options=session_options, provider_options=provider_options
            )
            kwargs["model_save_dir"] = Path(model_cache_path).parent
            kwargs["latest_model_name"] = Path(model_cache_path).name

        return cls(model=model, config=config, **kwargs)

    @classmethod
    def _from_transformers(
        cls,
        model_id: str,
        config: "PretrainedConfig",
        save_dir: Union[str, Path] = default_cache_path,
        use_auth_token: Optional[Union[bool, str]] = None,
        revision: Optional[str] = None,
        force_download: bool = False,
        cache_dir: Optional[str] = None,
        subfolder: str = "",
        local_files_only: bool = False,
        provider: str = "CPUExecutionProvider",
        session_options: Optional[ort.SessionOptions] = None,
        provider_options: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> "ORTModel":
        save_dir = Path(save_dir).joinpath(model_id, subfolder)
        save_dir.mkdir(parents=True, exist_ok=True)

        # Reads pipeline task from ORTModelForXXX class if available else tries to extract from hub
        if cls.export_feature is not None:
            task = cls.export_feature
        else:
            # TODO: Do we want to actually support that?
            # TODO: load from subfolder?
            task = TasksManager.infer_task_from_model(model_id, revision=revision)
            # TODO: is it still needed?
            if task in ["sentiment-analysis", "text-classification", "zero-shot-classification"]:
                task = "sequence-classification"
            elif task in ["feature-extraction", "fill-mask"]:
                task = "default"

        kwargs_to_get_model = {
            "subfolder": subfolder,
            "revision": revision,
        }

        model = TasksManager.get_model_from_task(task, model_id, **kwargs_to_get_model)
        model_type = model.config.model_type.replace("_", "-")
        onnx_config_class = TasksManager.get_exporter_config_constructor(
            model_type, "onnx", task=task, model_name=model_id
        )

        onnx_config = onnx_config_class(model.config)

        export(
            model=model,
            config=onnx_config,
            opset=onnx_config.DEFAULT_ONNX_OPSET,
            output=save_dir.joinpath(ONNX_WEIGHTS_NAME),
        )

        return cls._from_pretrained(save_dir.as_posix(), config, **kwargs)

    @classmethod
    @add_start_docstrings(FROM_PRETRAINED_START_DOCSTRING)
    def from_pretrained(
        cls,
        model_id: Union[str, Path],
        from_transformers: bool = False,
        force_download: bool = False,
        use_auth_token: Optional[str] = None,
        cache_dir: Optional[str] = None,
        subfolder: str = "",
        config: Optional["PretrainedConfig"] = None,
        local_files_only: bool = False,
        provider: str = "CPUExecutionProvider",
        session_options: Optional[ort.SessionOptions] = None,
        provider_options: Optional[Dict[str, Any]] = None,
        **kwargs,
    ):
        """
        provider (`str`, *optional*, defaults to `"CPUExecutionProvider"`):
            ONNX Runtime provider to use for loading the model. See https://onnxruntime.ai/docs/execution-providers/ for
            possible providers.
        session_options (`Optional[onnxruntime.SessionOptions]`, *optional*),:
            ONNX Runtime session options to use for loading the model.
        provider_options (`Optional[Dict[str, Any]]`, *optional*):
            Provider option dictionaries corresponding to the provider used. See available options
            for each provider: https://onnxruntime.ai/docs/api/c/group___global.html .
        kwargs (`Dict[str, Any]`):
            Will be passed to the underlying model loading methods.

        Returns:
            `ORTModel`: The loaded ORTModel model.
        """
        return super().from_pretrained(
            model_id,
            from_transformers=from_transformers,
            force_download=force_download,
            use_auth_token=use_auth_token,
            cache_dir=cache_dir,
            subfolder=subfolder,
            config=config,
            local_files_only=local_files_only,
            provider=provider,
            session_options=session_options,
            provider_options=provider_options,
            **kwargs,
        )


FEATURE_EXTRACTION_EXAMPLE = r"""
    Example of feature extraction:

    ```python
    >>> from transformers import {processor_class}
    >>> from optimum.onnxruntime import {model_class}
    >>> import torch

    >>> tokenizer = {processor_class}.from_pretrained("{checkpoint}")
    >>> model = {model_class}.from_pretrained("{checkpoint}")

    >>> inputs = tokenizer("My name is Philipp and I live in Germany.", return_tensors="pt")

    >>> outputs = model(**inputs)
    >>> logits = outputs.logits
    >>> list(logits.shape)
    ```

    Example using `transformers.pipeline`:

    ```python
    >>> from transformers import {processor_class}, pipeline
    >>> from optimum.onnxruntime import {model_class}

    >>> tokenizer = {processor_class}.from_pretrained("{checkpoint}")
    >>> model = {model_class}.from_pretrained("{checkpoint}")
    >>> onnx_extractor = pipeline("feature-extraction", model=model, tokenizer=tokenizer)

    >>> text = "My name is Philipp and I live in Germany."
    >>> pred = onnx_extractor(text)
    ```
"""


@add_start_docstrings(
    """
    Onnx Model with a MaskedLMOutput for feature-extraction tasks.
    """,
    ONNX_MODEL_START_DOCSTRING,
)
class ORTModelForFeatureExtraction(ORTModel):
    """
    Feature Extraction model for ONNX.
    """

    # used in from_transformers to export model to onnx
    export_feature = "default"
    auto_model_class = AutoModel

    def __init__(self, model=None, config=None, use_io_binding=True, **kwargs):
        super().__init__(model, config, use_io_binding, **kwargs)
        self.model_outputs = {output_key.name: idx for idx, output_key in enumerate(self.model.get_outputs())}
        self.name_to_np_type = TypeHelper.get_io_numpy_type_map(self.model) if self.use_io_binding else None

    def prepare_output_buffer(self, batch_size, sequence_length, hidden_size, output_name: str):
        """Prepares the buffer of output_name with a 1D tensor on shape: (batch_size, sequence_length, hidden_size)."""
        ort_type = TypeHelper.get_output_type(self.model, output_name)
        torch_type = TypeHelper.ort_type_to_torch_type(ort_type)

        output_shape = (batch_size, sequence_length, hidden_size)
        output_buffer = torch.empty(np.prod(output_shape), dtype=torch_type, device=self.device).contiguous()

        return output_shape, output_buffer

    def prepare_io_binding(
        self,
        input_ids: Optional[torch.Tensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        token_type_ids: Optional[torch.Tensor] = None,
    ):
        io_binding = self.model.io_binding()

        # bind input ids
        input_ids = input_ids.contiguous()
        io_binding.bind_input(
            "input_ids",
            input_ids.device.type,
            self.device.index,
            self.name_to_np_type["input_ids"],
            tuple(input_ids.shape),
            input_ids.data_ptr(),
        )
        # bind attention mask
        attention_mask = attention_mask.contiguous()
        io_binding.bind_input(
            "attention_mask",
            attention_mask.device.type,
            self.device.index,
            self.name_to_np_type["attention_mask"],
            tuple(attention_mask.shape),
            attention_mask.data_ptr(),
        )

        if token_type_ids is not None:
            # bind token type ids
            token_type_ids = token_type_ids.contiguous()
            io_binding.bind_input(
                "token_type_ids",
                token_type_ids.device.type,
                self.device.index,
                self.name_to_np_type["token_type_ids"],
                tuple(token_type_ids.shape),
                token_type_ids.data_ptr(),
            )

        # bind last_hidden_state
        output_shape, output_buffer = self.prepare_output_buffer(
            batch_size=input_ids.size(0),
            sequence_length=input_ids.size(1),
            hidden_size=self.config.hidden_size,
            output_name="last_hidden_state",
        )
        io_binding.bind_output(
            "last_hidden_state",
            output_buffer.device.type,
            self.device.index,
            self.name_to_np_type["last_hidden_state"],
            output_shape,
            output_buffer.data_ptr(),
        )
        output_shapes = {"last_hidden_state": output_shape}
        output_buffers = {"last_hidden_state": output_buffer}

        return io_binding, output_shapes, output_buffers

    @add_start_docstrings_to_model_forward(
        ONNX_TEXT_INPUTS_DOCSTRING.format("batch_size, sequence_length")
        + FEATURE_EXTRACTION_EXAMPLE.format(
            processor_class=_TOKENIZER_FOR_DOC,
            model_class="ORTModelForFeatureExtraction",
            checkpoint="optimum/all-MiniLM-L6-v2",
        )
    )
    def forward(
        self,
        input_ids: Optional[torch.Tensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        token_type_ids: Optional[torch.Tensor] = None,
        **kwargs,
    ):
        if self.device.type == "cuda" and self.use_io_binding:
            io_binding, output_shapes, output_buffers = self.prepare_io_binding(
                input_ids, attention_mask, token_type_ids
            )

            # run inference with binding & synchronize in case of multiple CUDA streams
            io_binding.synchronize_inputs()
            self.model.run_with_iobinding(io_binding)
            io_binding.synchronize_outputs()

            # converts output to namedtuple for pipelines post-processing
            return BaseModelOutput(
                last_hidden_state=output_buffers["last_hidden_state"].view(output_shapes["last_hidden_state"])
            )
        else:
            # converts pytorch inputs into numpy inputs for onnx
            onnx_inputs = {
                "input_ids": input_ids.cpu().detach().numpy(),
                "attention_mask": attention_mask.cpu().detach().numpy(),
            }
            if token_type_ids is not None:
                onnx_inputs["token_type_ids"] = token_type_ids.cpu().detach().numpy()

            # run inference
            outputs = self.model.run(None, onnx_inputs)
            last_hidden_state = torch.from_numpy(outputs[self.model_outputs["last_hidden_state"]]).to(self.device)

            # converts output to namedtuple for pipelines post-processing
            return BaseModelOutput(last_hidden_state=last_hidden_state)


QUESTION_ANSWERING_EXAMPLE = r"""
    Example of question answering:

    ```python
    >>> from transformers import {processor_class}
    >>> from optimum.onnxruntime import {model_class}
    >>> import torch

    >>> tokenizer = {processor_class}.from_pretrained("{checkpoint}")
    >>> model = {model_class}.from_pretrained("{checkpoint}")

    >>> question, text = "Who was Jim Henson?", "Jim Henson was a nice puppet"
    >>> inputs = tokenizer(question, text, return_tensors="pt")
    >>> start_positions = torch.tensor([1])
    >>> end_positions = torch.tensor([3])

    >>> outputs = model(**inputs, start_positions=start_positions, end_positions=end_positions)
    >>> start_scores = outputs.start_logits
    >>> end_scores = outputs.end_logits
    ```
    Example using `transformers.pipeline`:

    ```python
    >>> from transformers import {processor_class}, pipeline
    >>> from optimum.onnxruntime import {model_class}

    >>> tokenizer = {processor_class}.from_pretrained("{checkpoint}")
    >>> model = {model_class}.from_pretrained("{checkpoint}")
    >>> onnx_qa = pipeline("question-answering", model=model, tokenizer=tokenizer)

    >>> question, text = "Who was Jim Henson?", "Jim Henson was a nice puppet"
    >>> pred = onnx_qa(question, text)
    ```
"""


@add_start_docstrings(
    """
    Onnx Model with a QuestionAnsweringModelOutput for extractive question-answering tasks like SQuAD.
    """,
    ONNX_MODEL_START_DOCSTRING,
)
class ORTModelForQuestionAnswering(ORTModel):
    """
    Question Answering model for ONNX.
    """

    export_feature = "question-answering"
    auto_model_class = AutoModelForQuestionAnswering

    def __init__(self, model=None, config=None, use_io_binding=True, **kwargs):
        super().__init__(model, config, use_io_binding, **kwargs)
        self.model_outputs = {output_key.name: idx for idx, output_key in enumerate(self.model.get_outputs())}
        self.name_to_np_type = TypeHelper.get_io_numpy_type_map(self.model) if self.use_io_binding else None

    def prepare_logits_buffer(self, batch_size, sequence_length, output_name: str):
        """Prepares the buffer of logits with a 1D tensor on shape: (batch_size, sequence_length)."""
        ort_type = TypeHelper.get_output_type(self.model, output_name)
        torch_type = TypeHelper.ort_type_to_torch_type(ort_type)

        logits_shape = (batch_size, sequence_length)
        logits_buffer = torch.empty(np.prod(logits_shape), dtype=torch_type, device=self.device).contiguous()

        return logits_shape, logits_buffer

    def prepare_io_binding(
        self,
        input_ids: Optional[torch.Tensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        token_type_ids: Optional[torch.Tensor] = None,
    ):
        io_binding = self.model.io_binding()

        # bind input ids
        input_ids = input_ids.contiguous()
        io_binding.bind_input(
            "input_ids",
            input_ids.device.type,
            self.device.index,
            self.name_to_np_type["input_ids"],
            tuple(input_ids.shape),
            input_ids.data_ptr(),
        )
        # bind attention mask
        attention_mask = attention_mask.contiguous()
        io_binding.bind_input(
            "attention_mask",
            attention_mask.device.type,
            self.device.index,
            self.name_to_np_type["attention_mask"],
            tuple(attention_mask.shape),
            attention_mask.data_ptr(),
        )

        if token_type_ids is not None:
            # bind token type ids
            token_type_ids = token_type_ids.contiguous()
            io_binding.bind_input(
                "token_type_ids",
                token_type_ids.device.type,
                self.device.index,
                self.name_to_np_type["token_type_ids"],
                tuple(token_type_ids.shape),
                token_type_ids.data_ptr(),
            )

        # bind start_logits and end_logits
        start_logits_shape, start_logits_buffer = self.prepare_logits_buffer(
            batch_size=input_ids.size(0), sequence_length=input_ids.size(1), output_name="start_logits"
        )
        end_logits_shape, end_logits_buffer = self.prepare_logits_buffer(
            batch_size=input_ids.size(0), sequence_length=input_ids.size(1), output_name="end_logits"
        )
        io_binding.bind_output(
            "start_logits",
            start_logits_buffer.device.type,
            self.device.index,
            self.name_to_np_type["start_logits"],
            start_logits_shape,
            start_logits_buffer.data_ptr(),
        )
        io_binding.bind_output(
            "end_logits",
            end_logits_buffer.device.type,
            self.device.index,
            self.name_to_np_type["end_logits"],
            end_logits_shape,
            end_logits_buffer.data_ptr(),
        )
        output_shapes = {"start_logits": start_logits_shape, "end_logits": end_logits_shape}
        output_buffers = {"start_logits": start_logits_buffer, "end_logits": end_logits_buffer}

        return io_binding, output_shapes, output_buffers

    @add_start_docstrings_to_model_forward(
        ONNX_TEXT_INPUTS_DOCSTRING.format("batch_size, sequence_length")
        + QUESTION_ANSWERING_EXAMPLE.format(
            processor_class=_TOKENIZER_FOR_DOC,
            model_class="ORTModelForQuestionAnswering",
            checkpoint="optimum/roberta-base-squad2",
        )
    )
    def forward(
        self,
        input_ids: Optional[torch.Tensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        token_type_ids: Optional[torch.Tensor] = None,
        **kwargs,
    ):
        if self.device.type == "cuda" and self.use_io_binding:
            io_binding, output_shapes, output_buffers = self.prepare_io_binding(
                input_ids, attention_mask, token_type_ids
            )

            # run inference with binding & synchronize in case of multiple CUDA streams
            io_binding.synchronize_inputs()
            self.model.run_with_iobinding(io_binding)
            io_binding.synchronize_outputs()

            # map outputs with names
            start_logits = io_binding._iobinding.get_outputs()[0]
            end_logits = io_binding._iobinding.get_outputs()[1]

            # converts output to namedtuple for pipelines post-processing
            return QuestionAnsweringModelOutput(
                start_logits=output_buffers["start_logits"].view(output_shapes["start_logits"]),
                end_logits=output_buffers["end_logits"].view(output_shapes["end_logits"]),
            )
        else:
            # converts pytorch inputs into numpy inputs for onnx
            onnx_inputs = {
                "input_ids": input_ids.cpu().detach().numpy(),
                "attention_mask": attention_mask.cpu().detach().numpy(),
            }
            if token_type_ids is not None:
                onnx_inputs["token_type_ids"] = token_type_ids.cpu().detach().numpy()

            # run inference
            outputs = self.model.run(None, onnx_inputs)
            start_logits = torch.from_numpy(outputs[self.model_outputs["start_logits"]]).to(self.device)
            end_logits = torch.from_numpy(outputs[self.model_outputs["end_logits"]]).to(self.device)

            # converts output to namedtuple for pipelines post-processing
            return QuestionAnsweringModelOutput(start_logits=start_logits, end_logits=end_logits)


SEQUENCE_CLASSIFICATION_EXAMPLE = r"""
    Example of single-label classification:

    ```python
    >>> from transformers import {processor_class}
    >>> from optimum.onnxruntime import {model_class}
    >>> import torch

    >>> tokenizer = {processor_class}.from_pretrained("{checkpoint}")
    >>> model = {model_class}.from_pretrained("{checkpoint}")

    >>> inputs = tokenizer("Hello, my dog is cute", return_tensors="pt")

    >>> outputs = model(**inputs)
    >>> logits = outputs.logits
    >>> list(logits.shape)
    ```

    Example using `transformers.pipelines`:

    ```python
    >>> from transformers import {processor_class}, pipeline
    >>> from optimum.onnxruntime import {model_class}

    >>> tokenizer = {processor_class}.from_pretrained("{checkpoint}")
    >>> model = {model_class}.from_pretrained("{checkpoint}")
    >>> onnx_classifier = pipeline("text-classification", model=model, tokenizer=tokenizer)

    >>> text = "Hello, my dog is cute"
    >>> pred = onnx_classifier(text)
    ```

    Example using zero-shot-classification `transformers.pipelines`:

    ```python
    >>> from transformers import {processor_class}, pipeline
    >>> from optimum.onnxruntime import {model_class}

    >>> tokenizer = {processor_class}.from_pretrained("optimum/distilbert-base-uncased-mnli")
    >>> model = {model_class}.from_pretrained("optimum/distilbert-base-uncased-mnli")
    >>> onnx_z0 = pipeline("zero-shot-classification", model=model, tokenizer=tokenizer)

    >>> sequence_to_classify = "Who are you voting for in 2020?"
    >>> candidate_labels = ["Europe", "public health", "politics", "elections"]
    >>> pred = onnx_z0(sequence_to_classify, candidate_labels, multi_class=True)
    ```
"""


@add_start_docstrings(
    """
    Onnx Model with a sequence classification/regression head on top (a linear layer on top of the
    pooled output) e.g. for GLUE tasks.
    """,
    ONNX_MODEL_START_DOCSTRING,
)
class ORTModelForSequenceClassification(ORTModel):
    """
    Sequence Classification model for ONNX.
    """

    export_feature = "sequence-classification"
    auto_model_class = AutoModelForSequenceClassification

    def __init__(self, model=None, config=None, use_io_binding=True, **kwargs):
        super().__init__(model, config, use_io_binding, **kwargs)
        self.model_outputs = {output_key.name: idx for idx, output_key in enumerate(self.model.get_outputs())}
        self.model_inputs = {input_key.name: idx for idx, input_key in enumerate(self.model.get_inputs())}
        self.name_to_np_type = TypeHelper.get_io_numpy_type_map(self.model) if self.use_io_binding else None

    def prepare_logits_buffer(self, batch_size, num_labels):
        """Prepares the buffer of logits with a 1D tensor on shape: (batch_size, config.num_labels)."""
        ort_type = TypeHelper.get_output_type(self.model, "logits")
        torch_type = TypeHelper.ort_type_to_torch_type(ort_type)

        logits_shape = (batch_size, num_labels)
        logits_buffer = torch.empty(np.prod(logits_shape), dtype=torch_type, device=self.device).contiguous()

        return logits_shape, logits_buffer

    def prepare_io_binding(
        self,
        input_ids: Optional[torch.Tensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        token_type_ids: Optional[torch.Tensor] = None,
    ):
        io_binding = self.model.io_binding()

        # bind input ids
        input_ids = input_ids.contiguous()
        io_binding.bind_input(
            "input_ids",
            input_ids.device.type,
            self.device.index,
            self.name_to_np_type["input_ids"],
            tuple(input_ids.shape),
            input_ids.data_ptr(),
        )
        # bind attention mask
        attention_mask = attention_mask.contiguous()
        io_binding.bind_input(
            "attention_mask",
            attention_mask.device.type,
            self.device.index,
            self.name_to_np_type["attention_mask"],
            tuple(attention_mask.shape),
            attention_mask.data_ptr(),
        )

        if token_type_ids is not None:
            # bind token type ids
            token_type_ids = token_type_ids.contiguous()
            io_binding.bind_input(
                "token_type_ids",
                token_type_ids.device.type,
                self.device.index,
                self.name_to_np_type["token_type_ids"],
                tuple(token_type_ids.shape),
                token_type_ids.data_ptr(),
            )

        # bind logits
        logits_shape, logits_buffer = self.prepare_logits_buffer(
            batch_size=input_ids.size(0),
            num_labels=self.config.num_labels,
        )
        io_binding.bind_output(
            "logits",
            logits_buffer.device.type,
            self.device.index,
            self.name_to_np_type["logits"],
            logits_shape,
            logits_buffer.data_ptr(),
        )
        output_shapes = {"logits": logits_shape}
        output_buffers = {"logits": logits_buffer}

        return io_binding, output_shapes, output_buffers

    @add_start_docstrings_to_model_forward(
        ONNX_TEXT_INPUTS_DOCSTRING.format("batch_size, sequence_length")
        + SEQUENCE_CLASSIFICATION_EXAMPLE.format(
            processor_class=_TOKENIZER_FOR_DOC,
            model_class="ORTModelForSequenceClassification",
            checkpoint="optimum/distilbert-base-uncased-finetuned-sst-2-english",
        )
    )
    def forward(
        self,
        input_ids: Optional[torch.Tensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        token_type_ids: Optional[torch.Tensor] = None,
        **kwargs,
    ):
        if self.device.type == "cuda" and self.use_io_binding:
            io_binding, output_shapes, output_buffers = self.prepare_io_binding(
                input_ids, attention_mask, token_type_ids
            )

            # run inference with binding & synchronize in case of multiple CUDA streams
            io_binding.synchronize_inputs()
            self.model.run_with_iobinding(io_binding)
            io_binding.synchronize_outputs()

            # map outputs with names
            logits = io_binding._iobinding.get_outputs()[0]

            # converts output to namedtuple for pipelines post-processing
            return SequenceClassifierOutput(logits=output_buffers["logits"].view(output_shapes["logits"]))
        else:
            # converts pytorch inputs into numpy inputs for onnx
            onnx_inputs = {
                "input_ids": input_ids.cpu().detach().numpy(),
                "attention_mask": attention_mask.cpu().detach().numpy(),
            }
            if token_type_ids is not None:
                onnx_inputs["token_type_ids"] = token_type_ids.cpu().detach().numpy()

            # run inference
            outputs = self.model.run(None, onnx_inputs)
            logits = torch.from_numpy(outputs[self.model_outputs["logits"]]).to(self.device)

            # converts output to namedtuple for pipelines post-processing
            return SequenceClassifierOutput(logits=logits)


TOKEN_CLASSIFICATION_EXAMPLE = r"""
    Example of token classification:

    ```python
    >>> from transformers import {processor_class}
    >>> from optimum.onnxruntime import {model_class}
    >>> import torch

    >>> tokenizer = {processor_class}.from_pretrained("{checkpoint}")
    >>> model = {model_class}.from_pretrained("{checkpoint}")

    >>> inputs = tokenizer("My name is Philipp and I live in Germany.", return_tensors="pt")

    >>> outputs = model(**inputs)
    >>> logits = outputs.logits
    >>> list(logits.shape)
    ```

    Example using `transformers.pipelines`:

    ```python
    >>> from transformers import {processor_class}, pipeline
    >>> from optimum.onnxruntime import {model_class}

    >>> tokenizer = {processor_class}.from_pretrained("{checkpoint}")
    >>> model = {model_class}.from_pretrained("{checkpoint}")
    >>> onnx_ner = pipeline("token-classification", model=model, tokenizer=tokenizer)

    >>> text = "My name is Philipp and I live in Germany."
    >>> pred = onnx_ner(text)
    ```
"""


@add_start_docstrings(
    """
    Onnx Model with a token classification head on top (a linear layer on top of the hidden-states output) e.g.
    for Named-Entity-Recognition (NER) tasks.
    """,
    ONNX_MODEL_START_DOCSTRING,
)
class ORTModelForTokenClassification(ORTModel):
    """
    Token Classification model for ONNX.
    """

    export_feature = "token-classification"
    auto_model_class = AutoModelForTokenClassification

    def __init__(self, model=None, config=None, use_io_binding=True, **kwargs):
        super().__init__(model, config, use_io_binding, **kwargs)
        self.model_outputs = {output_key.name: idx for idx, output_key in enumerate(self.model.get_outputs())}
        self.name_to_np_type = TypeHelper.get_io_numpy_type_map(self.model) if self.use_io_binding else None

    def prepare_logits_buffer(self, batch_size, sequence_length, num_labels):
        """Prepares the buffer of logits with a 1D tensor on shape: (batch_size, sequence_length, config.num_labels)."""
        ort_type = TypeHelper.get_output_type(self.model, "logits")
        torch_type = TypeHelper.ort_type_to_torch_type(ort_type)

        logits_shape = (batch_size, sequence_length, num_labels)
        logits_buffer = torch.empty(np.prod(logits_shape), dtype=torch_type, device=self.device).contiguous()

        return logits_shape, logits_buffer

    def prepare_io_binding(
        self,
        input_ids: Optional[torch.Tensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        token_type_ids: Optional[torch.Tensor] = None,
    ):
        io_binding = self.model.io_binding()

        # bind input ids
        input_ids = input_ids.contiguous()
        io_binding.bind_input(
            "input_ids",
            input_ids.device.type,
            self.device.index,
            self.name_to_np_type["input_ids"],
            tuple(input_ids.shape),
            input_ids.data_ptr(),
        )
        # bind attention mask
        attention_mask = attention_mask.contiguous()
        io_binding.bind_input(
            "attention_mask",
            attention_mask.device.type,
            self.device.index,
            self.name_to_np_type["attention_mask"],
            tuple(attention_mask.shape),
            attention_mask.data_ptr(),
        )

        if token_type_ids is not None:
            # bind token type ids
            token_type_ids = token_type_ids.contiguous()
            io_binding.bind_input(
                "token_type_ids",
                token_type_ids.device.type,
                self.device.index,
                self.name_to_np_type["token_type_ids"],
                tuple(token_type_ids.shape),
                token_type_ids.data_ptr(),
            )

        # bind logits
        logits_shape, logits_buffer = self.prepare_logits_buffer(
            batch_size=input_ids.size(0),
            sequence_length=input_ids.size(1),
            num_labels=self.config.num_labels,
        )
        io_binding.bind_output(
            "logits",
            logits_buffer.device.type,
            self.device.index,
            self.name_to_np_type["logits"],
            logits_shape,
            logits_buffer.data_ptr(),
        )
        output_shapes = {"logits": logits_shape}
        output_buffers = {"logits": logits_buffer}

        return io_binding, output_shapes, output_buffers

    @add_start_docstrings_to_model_forward(
        ONNX_TEXT_INPUTS_DOCSTRING.format("batch_size, sequence_length")
        + TOKEN_CLASSIFICATION_EXAMPLE.format(
            processor_class=_TOKENIZER_FOR_DOC,
            model_class="ORTModelForTokenClassification",
            checkpoint="optimum/bert-base-NER",
        )
    )
    def forward(
        self,
        input_ids: Optional[torch.Tensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        token_type_ids: Optional[torch.Tensor] = None,
        **kwargs,
    ):
        if self.device.type == "cuda" and self.use_io_binding:
            io_binding, output_shapes, output_buffers = self.prepare_io_binding(
                input_ids, attention_mask, token_type_ids
            )

            # run inference with binding & synchronize in case of multiple CUDA streams
            io_binding.synchronize_inputs()
            self.model.run_with_iobinding(io_binding)
            io_binding.synchronize_outputs()

            # map outputs with names
            logits = io_binding._iobinding.get_outputs()[0]

            # converts output to namedtuple for pipelines post-processing
            return TokenClassifierOutput(logits=output_buffers["logits"].view(output_shapes["logits"]))
        else:
            # converts pytorch inputs into numpy inputs for onnx
            onnx_inputs = {
                "input_ids": input_ids.cpu().detach().numpy(),
                "attention_mask": attention_mask.cpu().detach().numpy(),
            }
            if token_type_ids is not None:
                onnx_inputs["token_type_ids"] = token_type_ids.cpu().detach().numpy()

            # run inference
            outputs = self.model.run(None, onnx_inputs)
            logits = torch.from_numpy(outputs[self.model_outputs["logits"]]).to(self.device)

            # converts output to namedtuple for pipelines post-processing
            return TokenClassifierOutput(logits=logits)


MULTIPLE_CHOICE_EXAMPLE = r"""
    Example of mutliple choice:

    ```python
    >>> from transformers import {processor_class}
    >>> from optimum.onnxruntime import {model_class}

    >>> tokenizer = {processor_class}.from_pretrained("{checkpoint}")
    >>> model = {model_class}.from_pretrained("{checkpoint}", from_transformers=True)

    >>> num_choices = 4
    >>> first_sentence = ["Members of the procession walk down the street holding small horn brass instruments."] * num_choices
    >>> second_sentence = [
    "A drum line passes by walking down the street playing their instruments.",
    "A drum line has heard approaching them.",
    "A drum line arrives and they're outside dancing and asleep.",
    "A drum line turns the lead singer watches the performance."
]
    >>> inputs = tokenizer(first_sentence, second_sentence, truncation=True, padding=True)
    # Unflatten the inputs values expanding it to the shape [batch_size, num_choices, seq_length]
    >>> for k, v in inputs.items():
    >>>     inputs[k] = [v[i: i + num_choices] for i in range(0, len(v), num_choices)]
    >>> inputs = dict(inputs.convert_to_tensors(tensor_type="pt"))
    >>> outputs = model(**inputs)
    >>> logits = outputs.logits
    ```
"""


@add_start_docstrings(
    """
    Onnx Model with a multiple choice classification head on top (a linear layer on top of the pooled output and a
    softmax) e.g. for RocStories/SWAG tasks.
    """,
    ONNX_MODEL_START_DOCSTRING,
)
class ORTModelForMultipleChoice(ORTModel):
    """
    Multiple choice model for ONNX.
    """

    export_feature = "multiple-choice"
    auto_model_class = AutoModelForMultipleChoice

    def __init__(self, model=None, config=None, use_io_binding=True, **kwargs):
        super().__init__(model, config, use_io_binding, **kwargs)
        self.model_outputs = {output_key.name: idx for idx, output_key in enumerate(self.model.get_outputs())}
        self.name_to_np_type = TypeHelper.get_io_numpy_type_map(self.model) if self.use_io_binding else None

    def prepare_logits_buffer(self, batch_size, num_choices):
        """Prepares the buffer of logits with a 1D tensor on shape: (batch_size, num_choices)."""
        ort_type = TypeHelper.get_output_type(self.model, "logits")
        torch_type = TypeHelper.ort_type_to_torch_type(ort_type)

        logits_shape = (batch_size, num_choices)
        logits_buffer = torch.empty(np.prod(logits_shape), dtype=torch_type, device=self.device).contiguous()

        return logits_shape, logits_buffer

    def prepare_io_binding(
        self,
        input_ids: Optional[torch.Tensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        token_type_ids: Optional[torch.Tensor] = None,
    ):
        io_binding = self.model.io_binding()

        # bind input ids
        input_ids = input_ids.contiguous()
        io_binding.bind_input(
            "input_ids",
            input_ids.device.type,
            self.device.index,
            self.name_to_np_type["input_ids"],
            tuple(input_ids.shape),
            input_ids.data_ptr(),
        )
        # bind attention mask
        attention_mask = attention_mask.contiguous()
        io_binding.bind_input(
            "attention_mask",
            attention_mask.device.type,
            self.device.index,
            self.name_to_np_type["attention_mask"],
            tuple(attention_mask.shape),
            attention_mask.data_ptr(),
        )

        if token_type_ids is not None:
            # bind token type ids
            token_type_ids = token_type_ids.contiguous()
            io_binding.bind_input(
                "token_type_ids",
                token_type_ids.device.type,
                self.device.index,
                self.name_to_np_type["token_type_ids"],
                tuple(token_type_ids.shape),
                token_type_ids.data_ptr(),
            )

        # bind logits
        logits_shape, logits_buffer = self.prepare_logits_buffer(
            batch_size=input_ids.size(0), num_choices=input_ids.size(1)
        )
        io_binding.bind_output(
            "logits",
            logits_buffer.device.type,
            self.device.index,
            self.name_to_np_type["logits"],
            logits_shape,
            logits_buffer.data_ptr(),
        )
        output_shapes = {"logits": logits_shape}
        output_buffers = {"logits": logits_buffer}

        return io_binding, output_shapes, output_buffers

    @add_start_docstrings_to_model_forward(
        ONNX_TEXT_INPUTS_DOCSTRING.format("batch_size, sequence_length")
        + MULTIPLE_CHOICE_EXAMPLE.format(
            processor_class=_TOKENIZER_FOR_DOC,
            model_class="ORTModelForMultipleChoice",
            checkpoint="ehdwns1516/bert-base-uncased_SWAG",
        )
    )
    def forward(
        self,
        input_ids: Optional[torch.Tensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        token_type_ids: Optional[torch.Tensor] = None,
        **kwargs,
    ):
        if self.device.type == "cuda" and self.use_io_binding:
            io_binding, output_shapes, output_buffers = self.prepare_io_binding(
                input_ids, attention_mask, token_type_ids
            )

            # run inference with binding & synchronize in case of multiple CUDA streams
            io_binding.synchronize_inputs()
            self.model.run_with_iobinding(io_binding)
            io_binding.synchronize_outputs()

            # converts output to namedtuple for pipelines post-processing
            return MultipleChoiceModelOutput(logits=output_buffers["logits"].view(output_shapes["logits"]))
        else:
            # converts pytorch inputs into numpy inputs for onnx
            onnx_inputs = {
                "input_ids": input_ids.cpu().detach().numpy(),
                "attention_mask": attention_mask.cpu().detach().numpy(),
            }
            if token_type_ids is not None:
                onnx_inputs["token_type_ids"] = token_type_ids.cpu().detach().numpy()

            # run inference
            outputs = self.model.run(None, onnx_inputs)
            logits = torch.from_numpy(outputs[self.model_outputs["logits"]]).to(self.device)

            # converts output to namedtuple for pipelines post-processing
            return MultipleChoiceModelOutput(logits=logits)


IMAGE_CLASSIFICATION_EXAMPLE = r"""
    Example of image classification:

    ```python
    >>> import requests
    >>> from PIL import Image
    >>> from optimum.onnxruntime import {model_class}
    >>> from transformers import {processor_class}

    >>> url = "http://images.cocodataset.org/val2017/000000039769.jpg"
    >>> image = Image.open(requests.get(url, stream=True).raw)

    >>> preprocessor = {processor_class}.from_pretrained("{checkpoint}")
    >>> model = {model_class}.from_pretrained("{checkpoint}")

    >>> inputs = preprocessor(images=image, return_tensors="pt")

    >>> outputs = model(**inputs)
    >>> logits = outputs.logits
    ```

    Example using `transformers.pipeline`:

    ```python
    >>> import requests
    >>> from PIL import Image
    >>> from transformers import {processor_class}, pipeline
    >>> from optimum.onnxruntime import {model_class}

    >>> preprocessor = {processor_class}.from_pretrained("{checkpoint}")
    >>> model = {model_class}.from_pretrained("{checkpoint}")
    >>> onnx_image_classifier = pipeline("image-classification", model=model, feature_extractor=preprocessor)

    >>> url = "http://images.cocodataset.org/val2017/000000039769.jpg"
    >>> pred = onnx_image_classifier(url)
    ```
"""


@add_start_docstrings(
    """
    Onnx Model for image-classification tasks.
    """,
    ONNX_MODEL_START_DOCSTRING,
)
class ORTModelForImageClassification(ORTModel):
    """
    Image Classification model for ONNX.
    """

    export_feature = "image-classification"
    auto_model_class = AutoModelForImageClassification

    def __init__(self, model=None, config=None, use_io_binding=True, **kwargs):
        super().__init__(model, config, use_io_binding, **kwargs)
        self.model_outputs = {output_key.name: idx for idx, output_key in enumerate(self.model.get_outputs())}
        self.name_to_np_type = TypeHelper.get_io_numpy_type_map(self.model) if self.use_io_binding else None

    def prepare_logits_buffer(self, batch_size):
        """Prepares the buffer of logits with a 1D tensor on shape: (batch_size, config.num_labels)."""
        ort_type = TypeHelper.get_output_type(self.model, "logits")
        torch_type = TypeHelper.ort_type_to_torch_type(ort_type)

        logits_shape = (batch_size, self.config.num_labels)
        logits_buffer = torch.empty(np.prod(logits_shape), dtype=torch_type, device=self.device).contiguous()

        return logits_shape, logits_buffer

    def prepare_io_binding(
        self,
        pixel_values: torch.Tensor,
    ):
        io_binding = self.model.io_binding()

        # bind pixel values
        pixel_values = pixel_values.contiguous()
        io_binding.bind_input(
            "pixel_values",
            pixel_values.device.type,
            self.device.index,
            self.name_to_np_type["pixel_values"],
            tuple(pixel_values.shape),
            pixel_values.data_ptr(),
        )

        # bind logits
        logits_shape, logits_buffer = self.prepare_logits_buffer(batch_size=pixel_values.size(0))
        io_binding.bind_output(
            "logits",
            logits_buffer.device.type,
            self.device.index,
            self.name_to_np_type["logits"],
            logits_shape,
            logits_buffer.data_ptr(),
        )
        output_shapes = {"logits": logits_shape}
        output_buffers = {"logits": logits_buffer}

        return io_binding, output_shapes, output_buffers

    @add_start_docstrings_to_model_forward(
        ONNX_IMAGE_INPUTS_DOCSTRING.format("batch_size, num_channels, height, width")
        + IMAGE_CLASSIFICATION_EXAMPLE.format(
            processor_class=_FEATURE_EXTRACTOR_FOR_DOC,
            model_class="ORTModelForImageClassification",
            checkpoint="optimum/vit-base-patch16-224",
        )
    )
    def forward(
        self,
        pixel_values: torch.Tensor,
        **kwargs,
    ):
        if self.device.type == "cuda" and self.use_io_binding:
            io_binding, output_shapes, output_buffers = self.prepare_io_binding(pixel_values)

            # run inference with binding & synchronize in case of multiple CUDA streams
            io_binding.synchronize_inputs()
            self.model.run_with_iobinding(io_binding)
            io_binding.synchronize_outputs()

            # converts output to namedtuple for pipelines post-processing
            return ImageClassifierOutput(logits=output_buffers["logits"].view(output_shapes["logits"]))
        else:
            # converts pytorch inputs into numpy inputs for onnx
            onnx_inputs = {
                "pixel_values": pixel_values.cpu().detach().numpy(),
            }

            # run inference
            outputs = self.model.run(None, onnx_inputs)
            logits = torch.from_numpy(outputs[self.model_outputs["logits"]])

            # converts output to namedtuple for pipelines post-processing
            return ImageClassifierOutput(logits=logits)


CUSTOM_TASKS_EXAMPLE = r"""
    Example of custom tasks(e.g. a sentence transformers taking `pooler_output` as output):

    ```python
    >>> from transformers import {processor_class}
    >>> from optimum.onnxruntime import {model_class}

    >>> tokenizer = {processor_class}.from_pretrained("{checkpoint}")
    >>> model = {model_class}.from_pretrained("{checkpoint}")

    >>> inputs = tokenizer("I love burritos!", return_tensors="pt")

    >>> outputs = model(**inputs)
    >>> last_hidden_state = outputs.last_hidden_state
    >>> pooler_output = outputs.pooler_output
    ```

    Example using `transformers.pipelines`(only if the task is supported):

    ```python
    >>> from transformers import {processor_class}, pipeline
    >>> from optimum.onnxruntime import {model_class}

    >>> tokenizer = {processor_class}.from_pretrained("{checkpoint}")
    >>> model = {model_class}.from_pretrained("{checkpoint}")
    >>> onnx_extractor = pipeline("feature-extraction", model=model, tokenizer=tokenizer)

    >>> text = "I love burritos!"
    >>> pred = onnx_extractor(text)
    ```
"""


@add_start_docstrings(
    """
    Onnx Model for any custom tasks. It can be used to leverage the inference acceleration with any custom exported ONNX model.
    """,
    ONNX_MODEL_START_DOCSTRING,
)
class ORTModelForCustomTasks(ORTModel):
    """
    Onnx Model for any custom tasks.
    """

    export_feature = "default"
    auto_model_class = AutoModel

    def __init__(self, model=None, config=None, **kwargs):
        super().__init__(model, config, **kwargs)
        if kwargs.pop("use_io_binding", False):
            logger.warning(
                "ORTModelForCustomTasks doesn't support IO Binding yet, and the inference will be done without IO binding which could cause"
                " significant overhead on data copying. If you want us to enable IO binding for custom use case, please open an issue in "
                "Optimum: https://github.com/huggingface/optimum."
            )

    @add_start_docstrings_to_model_forward(
        CUSTOM_TASKS_EXAMPLE.format(
            processor_class=_TOKENIZER_FOR_DOC,
            model_class="ORTModelForCustomTasks",
            checkpoint="optimum/sbert-all-MiniLM-L6-with-pooler",
        )
    )
    def forward(self, **kwargs):
        # converts pytorch inputs into numpy inputs for onnx
        onnx_inputs = self._prepare_onnx_inputs(**kwargs)
        # run inference
        onnx_outputs = self.model.run(None, onnx_inputs)
        outputs = self._prepare_onnx_outputs(onnx_outputs)
        # converts outputs to namedtuple for pipelines post-processing if applicable
        return ModelOutput(outputs)

    def _prepare_onnx_inputs(self, **kwargs):
        model_inputs = {input_key.name: idx for idx, input_key in enumerate(self.model.get_inputs())}
        onnx_inputs = {}
        # converts pytorch inputs into numpy inputs for onnx
        for input in model_inputs.keys():
            onnx_inputs[input] = kwargs.pop(input).cpu().detach().numpy()

        return onnx_inputs

    def _prepare_onnx_outputs(self, onnx_outputs):
        model_outputs = {output_key.name: idx for idx, output_key in enumerate(self.model.get_outputs())}
        outputs = {}
        # converts onnxruntime outputs into tensor for standard outputs
        for output, idx in model_outputs.items():
            outputs[output] = torch.from_numpy(onnx_outputs[idx]).to(self.device)

        return outputs
