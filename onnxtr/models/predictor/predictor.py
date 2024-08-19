# Copyright (C) 2021-2024, Mindee | Felix Dittrich.

# This program is licensed under the Apache License 2.0.
# See LICENSE or go to <https://opensource.org/licenses/Apache-2.0> for full license details.

from typing import Any, List, Optional

import numpy as np

from onnxtr.io.elements import Document
from onnxtr.models._utils import get_language
from onnxtr.models.detection.predictor import DetectionPredictor
from onnxtr.models.engine import EngineConfig
from onnxtr.models.recognition.predictor import RecognitionPredictor
from onnxtr.utils.geometry import detach_scores
from onnxtr.utils.repr import NestedObject

from .base import _OCRPredictor

__all__ = ["OCRPredictor"]


class OCRPredictor(NestedObject, _OCRPredictor):
    """Implements an object able to localize and identify text elements in a set of documents

    Args:
    ----
        det_predictor: detection module
        reco_predictor: recognition module
        assume_straight_pages: if True, speeds up the inference by assuming you only pass straight pages
            without rotated textual elements.
        straighten_pages: if True, estimates the page general orientation based on the median line orientation.
            Then, rotates page before passing it to the deep learning modules. The final predictions will be remapped
            accordingly. Doing so will improve performances for documents with page-uniform rotations.
        detect_orientation: if True, the estimated general page orientation will be added to the predictions for each
            page. Doing so will slightly deteriorate the overall latency.
        detect_language: if True, the language prediction will be added to the predictions for each
            page. Doing so will slightly deteriorate the overall latency.
        clf_engine_cfg: configuration of the orientation classification engine
        **kwargs: keyword args of `DocumentBuilder`
    """

    _children_names = ["det_predictor", "reco_predictor", "doc_builder"]

    def __init__(
        self,
        det_predictor: DetectionPredictor,
        reco_predictor: RecognitionPredictor,
        assume_straight_pages: bool = True,
        straighten_pages: bool = False,
        preserve_aspect_ratio: bool = True,
        symmetric_pad: bool = True,
        detect_orientation: bool = False,
        detect_language: bool = False,
        clf_engine_cfg: Optional[EngineConfig] = None,
        **kwargs: Any,
    ) -> None:
        self.det_predictor = det_predictor
        self.reco_predictor = reco_predictor
        _OCRPredictor.__init__(
            self,
            assume_straight_pages,
            straighten_pages,
            preserve_aspect_ratio,
            symmetric_pad,
            detect_orientation,
            clf_engine_cfg=clf_engine_cfg,
            **kwargs,
        )
        self.detect_orientation = detect_orientation
        self.detect_language = detect_language

    def __call__(
        self,
        pages: List[np.ndarray],
        **kwargs: Any,
    ) -> Document:
        # Dimension check
        if any(page.ndim != 3 for page in pages):
            raise ValueError("incorrect input shape: all pages are expected to be multi-channel 2D images.")

        origin_page_shapes = [page.shape[:2] for page in pages]

        # Localize text elements
        loc_preds, out_maps = self.det_predictor(pages, return_maps=True, **kwargs)

        # Detect document rotation and rotate pages
        seg_maps = [
            np.where(out_map > getattr(self.det_predictor.model.postprocessor, "bin_thresh"), 255, 0).astype(np.uint8)
            for out_map in out_maps
        ]
        if self.detect_orientation:
            general_pages_orientations, origin_pages_orientations = self._get_orientations(pages, seg_maps)
            orientations = [
                {"value": orientation_page, "confidence": None} for orientation_page in origin_pages_orientations
            ]
        else:
            orientations = None
            general_pages_orientations = None
            origin_pages_orientations = None
        if self.straighten_pages:
            pages = self._straighten_pages(pages, seg_maps, general_pages_orientations, origin_pages_orientations)
            # update page shapes after straightening
            origin_page_shapes = [page.shape[:2] for page in pages]

            # forward again to get predictions on straight pages
            loc_preds = self.det_predictor(pages, **kwargs)  # type: ignore[assignment]

        # Detach objectness scores from loc_preds
        loc_preds, objectness_scores = detach_scores(loc_preds)  # type: ignore[arg-type]

        # Apply hooks to loc_preds if any
        for hook in self.hooks:
            loc_preds = hook(loc_preds)

        # Crop images
        crops, loc_preds = self._prepare_crops(
            pages,
            loc_preds,  # type: ignore[arg-type]
            channels_last=True,
            assume_straight_pages=self.assume_straight_pages,
        )
        # Rectify crop orientation and get crop orientation predictions
        crop_orientations: Any = []
        if not self.assume_straight_pages:
            crops, loc_preds, _crop_orientations = self._rectify_crops(crops, loc_preds)
            crop_orientations = [
                {"value": orientation[0], "confidence": orientation[1]} for orientation in _crop_orientations
            ]

        # Identify character sequences
        word_preds = self.reco_predictor([crop for page_crops in crops for crop in page_crops], **kwargs)
        if not crop_orientations:
            crop_orientations = [{"value": 0, "confidence": None} for _ in word_preds]

        boxes, text_preds, crop_orientations = self._process_predictions(loc_preds, word_preds, crop_orientations)

        if self.detect_language:
            languages = [get_language(" ".join([item[0] for item in text_pred])) for text_pred in text_preds]
            languages_dict = [{"value": lang[0], "confidence": lang[1]} for lang in languages]
        else:
            languages_dict = None

        out = self.doc_builder(
            pages,
            boxes,
            objectness_scores,
            text_preds,
            origin_page_shapes,  # type: ignore[arg-type]
            crop_orientations,
            orientations,
            languages_dict,
        )
        return out
