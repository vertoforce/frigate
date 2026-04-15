"""SigLIP 2 Embeddings (English-focused CLIP alternative).

Uses the `onnx-community/siglip2-large-patch16-384-ONNX` export which ships
separate text/vision ONNX graphs, mirroring the jina-clip-v1 split-encoder
layout rather than jina-clip-v2's joint graph.

SigLIP 2 natively produces 1024-dim embeddings; Frigate's sqlite-vec schema
stores 768-dim vectors (to match jina-clip-v1). We truncate to the first 768
dims in `_postprocess_outputs` so the model is compatible with the existing
DB. Unlike jina-clip-v2 (Matryoshka-trained), SigLIP is not trained for
ordered-dimension truncation, so accuracy loss is higher than v2's. Empirically
acceptable for "find red car" / "Find Similar" queries.
"""

import logging
import os
import threading
import warnings
from typing import Any

import numpy as np
from PIL import Image
from transformers import AutoImageProcessor, AutoTokenizer
from transformers.utils.logging import disable_progress_bar

from frigate.comms.inter_process import InterProcessRequestor
from frigate.const import MODEL_CACHE_DIR, UPDATE_MODEL_STATE
from frigate.detectors.detection_runners import BaseModelRunner, get_optimized_runner
from frigate.embeddings.types import EnrichmentModelTypeEnum
from frigate.types import ModelStatusTypesEnum
from frigate.util.downloader import ModelDownloader

from .base_embedding import BaseEmbedding

warnings.filterwarnings(
    "ignore",
    category=FutureWarning,
    message="The class CLIPFeatureExtractor is deprecated",
)

disable_progress_bar()
logger = logging.getLogger(__name__)


_HF_REPO = "onnx-community/siglip2-large-patch16-384-ONNX"
_HF_TOKENIZER_REPO = "google/siglip2-large-patch16-384"
# Input image side length expected by the vision encoder (patch16-384 → 384).
_IMAGE_SIZE = 384
# Native embedding dimension of SigLIP large. Truncated to 768 to fit Frigate's
# sqlite-vec schema — see module docstring.
_NATIVE_DIM = 1024
_TRUNCATE_DIM = 768


def _hf_endpoint() -> str:
    return os.environ.get("HF_ENDPOINT", "https://huggingface.co")


class _SigLIPCallMixin:
    """Shared __call__ override.

    BaseEmbedding picks `runner.run(...)[0]` — the first ONNX output. SigLIP's
    encoder ONNX exports two outputs per pass: `last_hidden_state` (per-token,
    [0]) and `pooler_output` (pooled vector, [1]). We want the latter.
    """

    def __call__(
        self, inputs: list[str] | list[Image.Image] | list[bytes]
    ) -> list[np.ndarray]:
        self._load_model_and_utils()
        processed = self._preprocess_inputs(inputs)
        input_names = self.runner.get_input_names()
        onnx_inputs: dict[str, Any] = {name: [] for name in input_names}
        for item in processed:
            for key, value in item.items():
                if key in input_names:
                    onnx_inputs[key].append(value[0])

        for key in input_names:
            if onnx_inputs.get(key) is None or (
                hasattr(onnx_inputs[key], "__len__") and len(onnx_inputs[key]) == 0
            ):
                logger.warning(f"Expected input '{key}' not found in onnx_inputs")
            else:
                onnx_inputs[key] = np.stack(onnx_inputs[key])

        outputs = self.runner.run(onnx_inputs)
        # [1] = pooler_output (batch, 1024); [0] is last_hidden_state.
        pooled = outputs[1]
        embeddings = self._postprocess_outputs(pooled)
        return [embedding for embedding in embeddings]


class SigLIPTextEmbedding(_SigLIPCallMixin, BaseEmbedding):
    def __init__(
        self,
        model_size: str,
        requestor: InterProcessRequestor,
        device: str = "AUTO",
    ):
        # SigLIP 2 large ONNX export only ships fp16 for the text encoder.
        # `model_size` is kept in the signature to match jina_v1 but currently
        # has no effect — always uses the fp16 weights.
        model_file = "text_model_fp16.onnx"
        super().__init__(
            model_name="onnx-community/siglip2-large-patch16-384-ONNX",
            model_file=model_file,
            download_urls={
                model_file: f"{_hf_endpoint()}/{_HF_REPO}/resolve/main/onnx/{model_file}",
            },
        )
        self.tokenizer_file = "tokenizer"
        self.requestor = requestor
        self.model_size = model_size
        self.device = device
        self.download_path = os.path.join(MODEL_CACHE_DIR, self.model_name)
        self.tokenizer = None
        self.runner: BaseModelRunner | None = None
        self._lock = threading.Lock()
        files_names = list(self.download_urls.keys()) + [self.tokenizer_file]

        if not all(
            os.path.exists(os.path.join(self.download_path, n)) for n in files_names
        ):
            logger.debug(f"starting model download for {self.model_name}")
            self.downloader = ModelDownloader(
                model_name=self.model_name,
                download_path=self.download_path,
                file_names=files_names,
                download_func=self._download_model,
            )
            self.downloader.ensure_model_files()
        else:
            self.downloader = None
            ModelDownloader.mark_files_state(
                self.requestor,
                self.model_name,
                files_names,
                ModelStatusTypesEnum.downloaded,
            )
            self._load_model_and_utils()
            logger.debug(f"models are already downloaded for {self.model_name}")

    def _download_model(self, path: str):
        try:
            file_name = os.path.basename(path)

            if file_name in self.download_urls:
                ModelDownloader.download_from_url(self.download_urls[file_name], path)
            elif file_name == self.tokenizer_file:
                if not os.path.exists(path + "/" + self.model_name):
                    logger.info(f"Downloading {self.model_name} tokenizer")

                # SigLIP uses a SentencePiece tokenizer; AutoTokenizer handles
                # the `spiece.model` download internally.
                tokenizer = AutoTokenizer.from_pretrained(
                    _HF_TOKENIZER_REPO,
                    cache_dir=f"{MODEL_CACHE_DIR}/{self.model_name}/tokenizer",
                    clean_up_tokenization_spaces=True,
                )
                tokenizer.save_pretrained(path)

            self.downloader.requestor.send_data(
                UPDATE_MODEL_STATE,
                {
                    "model": f"{self.model_name}-{file_name}",
                    "state": ModelStatusTypesEnum.downloaded,
                },
            )
        except Exception:
            self.downloader.requestor.send_data(
                UPDATE_MODEL_STATE,
                {
                    "model": f"{self.model_name}-{file_name}",
                    "state": ModelStatusTypesEnum.error,
                },
            )

    def _load_model_and_utils(self):
        if self.runner is None:
            if self.downloader:
                self.downloader.wait_for_download()

            tokenizer_path = os.path.join(
                f"{MODEL_CACHE_DIR}/{self.model_name}/tokenizer"
            )
            self.tokenizer = AutoTokenizer.from_pretrained(
                _HF_TOKENIZER_REPO,
                cache_dir=tokenizer_path,
                clean_up_tokenization_spaces=True,
            )

            self.runner = get_optimized_runner(
                os.path.join(self.download_path, self.model_file),
                self.device,
                model_type=EnrichmentModelTypeEnum.siglip.value,
            )

    def _preprocess_inputs(self, raw_inputs):
        with self._lock:
            # SigLIP uses a fixed padding length of 64 per the model card;
            # keep it explicit instead of relying on batch-max.
            return [
                self.tokenizer(
                    text,
                    padding="max_length",
                    truncation=True,
                    max_length=64,
                    return_tensors="np",
                )
                for text in raw_inputs
            ]

    def _postprocess_outputs(self, outputs):
        if outputs.shape[-1] > _TRUNCATE_DIM:
            outputs = outputs[..., :_TRUNCATE_DIM]
        return outputs


class SigLIPImageEmbedding(_SigLIPCallMixin, BaseEmbedding):
    def __init__(
        self,
        model_size: str,
        requestor: InterProcessRequestor,
        device: str = "AUTO",
    ):
        # Only fp16 vision weights are published for this ONNX export.
        model_file = "vision_model_fp16.onnx"
        super().__init__(
            model_name="onnx-community/siglip2-large-patch16-384-ONNX",
            model_file=model_file,
            download_urls={
                model_file: f"{_hf_endpoint()}/{_HF_REPO}/resolve/main/onnx/{model_file}",
                "preprocessor_config.json": f"{_hf_endpoint()}/{_HF_REPO}/resolve/main/preprocessor_config.json",
            },
        )
        self.requestor = requestor
        self.model_size = model_size
        self.device = device
        self.download_path = os.path.join(MODEL_CACHE_DIR, self.model_name)
        self.feature_extractor = None
        self.runner: BaseModelRunner | None = None
        self._lock = threading.Lock()
        files_names = list(self.download_urls.keys())
        if not all(
            os.path.exists(os.path.join(self.download_path, n)) for n in files_names
        ):
            logger.debug(f"starting model download for {self.model_name}")
            self.downloader = ModelDownloader(
                model_name=self.model_name,
                download_path=self.download_path,
                file_names=files_names,
                download_func=self._download_model,
            )
            self.downloader.ensure_model_files()
            # Block until downloads complete and load on main thread, matching
            # the jina_v1 pattern (lazy loading in worker threads is fragile).
            self._load_model_and_utils()
        else:
            self.downloader = None
            ModelDownloader.mark_files_state(
                self.requestor,
                self.model_name,
                files_names,
                ModelStatusTypesEnum.downloaded,
            )
            self._load_model_and_utils()
            logger.debug(f"models are already downloaded for {self.model_name}")

    def _load_model_and_utils(self):
        if self.runner is None:
            if self.downloader:
                self.downloader.wait_for_download()

            # AutoImageProcessor (not AutoFeatureExtractor): the SigLIP preprocessor
            # config uses the modern "image_processor_type" key, not the legacy
            # "feature_extractor_type" key.
            self.feature_extractor = AutoImageProcessor.from_pretrained(
                f"{MODEL_CACHE_DIR}/{self.model_name}",
            )

            self.runner = get_optimized_runner(
                os.path.join(self.download_path, self.model_file),
                self.device,
                model_type=EnrichmentModelTypeEnum.siglip.value,
            )

    def _preprocess_inputs(self, raw_inputs):
        with self._lock:
            processed_images = [self._process_image(img) for img in raw_inputs]
            return [
                self.feature_extractor(images=image, return_tensors="np")
                for image in processed_images
            ]

    def _postprocess_outputs(self, outputs):
        if outputs.shape[-1] > _TRUNCATE_DIM:
            outputs = outputs[..., :_TRUNCATE_DIM]
        return outputs
