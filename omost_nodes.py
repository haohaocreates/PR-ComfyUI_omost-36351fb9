from __future__ import annotations
import json
from typing import Literal, Tuple, TypedDict, NamedTuple
import sys
import logging
from typing_extensions import NotRequired

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

import comfy.model_management
from comfy.sd import CLIP
from nodes import CLIPTextEncode, ConditioningSetMask
from .lib_omost.canvas import (
    Canvas as OmostCanvas,
    OmostCanvasCondition,
    system_prompt,
)
from .lib_omost.utils import numpy2pytorch


def create_logger(level=logging.INFO):
    logger = logging.getLogger(__name__)
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        logger.addHandler(handler)
    return logger


logger = create_logger(level=logging.DEBUG)


# Type definitions.
class OmostConversationItem(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


OmostConversation = list[OmostConversationItem]


class OmostLLM(NamedTuple):
    model: AutoModelForCausalLM
    tokenizer: AutoTokenizer


ComfyUIConditioning = list  # Dummy type definitions for ComfyUI
CLIPTokensWithWeight = list[Tuple[int, float]]


class CLIPTokens(TypedDict):
    l: list[CLIPTokensWithWeight]
    g: NotRequired[list[CLIPTokensWithWeight]]


# End of type definitions.


class OmostLLMLoaderNode:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "llm_name": (
                    [
                        "lllyasviel/omost-phi-3-mini-128k-8bits",
                        "lllyasviel/omost-llama-3-8b-4bits",
                        "lllyasviel/omost-dolphin-2.9-llama3-8b-4bits",
                    ],
                    {
                        "default": "lllyasviel/omost-llama-3-8b-4bits",
                    },
                ),
            }
        }

    RETURN_TYPES = ("OMOST_LLM",)
    FUNCTION = "load_llm"

    def load_llm(self, llm_name: str) -> Tuple[OmostLLM]:
        """Load LLM model"""
        HF_TOKEN = None
        dtype = (
            torch.float16 if comfy.model_management.should_use_fp16() else torch.float32
        )

        llm_model = AutoModelForCausalLM.from_pretrained(
            llm_name,
            torch_dtype=dtype,  # This is computation type, not load/memory type. The loading quant type is baked in config.
            token=HF_TOKEN,
            device_map="auto",  # This will load model to gpu with an offload system
        )
        llm_tokenizer = AutoTokenizer.from_pretrained(llm_name, token=HF_TOKEN)

        return (OmostLLM(llm_model, llm_tokenizer),)


class OmostLLMChatNode:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "llm": ("OMOST_LLM",),
                "text": ("STRING", {"multiline": True}),
                "max_new_tokens": (
                    "INT",
                    {"min": 128, "max": 4096, "step": 1, "default": 4096},
                ),
                "top_p": (
                    "FLOAT",
                    {"min": 0.0, "max": 1.0, "step": 0.01, "default": 0.9},
                ),
                "temperature": (
                    "FLOAT",
                    {"min": 0.0, "max": 2.0, "step": 0.01, "default": 0.6},
                ),
            },
            "optional": {
                "conversation": ("OMOST_CONVERSATION",),
            },
        }

    RETURN_TYPES = (
        "OMOST_CONVERSATION",
        "OMOST_CANVAS_CONDITIONING",
    )
    FUNCTION = "run_llm"

    def run_llm(
        self,
        llm: OmostLLM,
        text: str,
        max_new_tokens: int,
        top_p: float,
        temperature: float,
        conversation: OmostConversation | None = None,
    ) -> Tuple[OmostConversation, OmostCanvas]:
        """Run LLM on text"""
        llm_tokenizer: AutoTokenizer = llm.tokenizer
        llm_model: AutoModelForCausalLM = llm.model

        conversation = conversation or []  # Default to empty list
        system_conversation_item: OmostConversationItem = {
            "role": "system",
            "content": system_prompt,
        }
        user_conversation_item: OmostConversationItem = {
            "role": "user",
            "content": text,
        }
        input_conversation: list[OmostConversationItem] = [
            system_conversation_item,
            *conversation,
            user_conversation_item,
        ]

        input_ids: torch.Tensor = llm_tokenizer.apply_chat_template(
            input_conversation, return_tensors="pt", add_generation_prompt=True
        ).to(llm_model.device)
        input_length = input_ids.shape[1]

        output_ids: torch.Tensor = llm_model.generate(
            input_ids=input_ids,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
            do_sample=temperature != 0,
        )
        generated_ids = output_ids[:, input_length:]
        generated_text: str = llm_tokenizer.decode(
            generated_ids[0],
            skip_special_tokens=True,
            skip_prompt=True,
            timeout=10,
        )

        output_conversation = [
            *conversation,
            user_conversation_item,
            {"role": "assistant", "content": generated_text},
        ]
        return (
            output_conversation,
            OmostCanvas.from_bot_response(generated_text).process(),
        )


class OmostRenderCanvasConditioningNode:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "canvas_conds": ("OMOST_CANVAS_CONDITIONING",),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "render_canvas"

    def render_canvas(
        self, canvas_conds: list[OmostCanvasCondition]
    ) -> Tuple[torch.Tensor]:
        """Render canvas conditioning to image"""
        return (numpy2pytorch(imgs=[OmostCanvas.render_initial_latent(canvas_conds)]),)


class OmostLayoutCondNode:
    """Apply Omost layout with ComfyUI's area condition system."""

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "canvas_conds": ("OMOST_CANVAS_CONDITIONING",),
                "clip": ("CLIP",),
                "global_strength": (
                    "FLOAT",
                    {"min": 0.0, "max": 1.0, "step": 0.01, "default": 0.2},
                ),
                "region_strength": (
                    "FLOAT",
                    {"min": 0.0, "max": 1.0, "step": 0.01, "default": 0.8},
                ),
            },
            "optional": {
                "positive": ("CONDITIONING",),
            },
        }

    RETURN_TYPES = ("CONDITIONING",)
    FUNCTION = "layout_cond"

    def __init__(self):
        self.cond_set_mask_node = ConditioningSetMask()
        self.clip_text_encode_node = CLIPTextEncode()

    def encode_bag_of_subprompts(
        self, clip: CLIP, prefixes: list[str], suffixes: list[str]
    ) -> ComfyUIConditioning:
        """Simplified way to encode bag of subprompts without omost's greedy approach."""
        conds: ComfyUIConditioning = []

        logger.debug("Start encoding bag of subprompts")
        for target in suffixes:
            complete_prompt = "".join(prefixes + [target])
            logger.debug(f"Encoding prompt: {complete_prompt}")
            cond: ComfyUIConditioning = self.clip_text_encode_node.encode(
                clip, complete_prompt
            )[0]
            assert len(cond) == 1
            conds.extend(cond)

        logger.debug("End encoding bag of subprompts. Total conditions: %d", len(conds))

        # Concat all conditions
        return [
            [
                # cond
                torch.cat([cond for cond, _ in conds], dim=1),
                # extra_dict
                {"pooled_output": conds[0][1]["pooled_output"]},
            ]
        ]

    @staticmethod
    def calc_cond_mask(
        canvas_conds: list[OmostCanvasCondition],
    ) -> list[OmostCanvasCondition]:
        """Calculate canvas cond mask."""
        CANVAS_SIZE = 90
        assert len(canvas_conds) > 0
        canvas_conds = canvas_conds.copy()

        global_cond = canvas_conds[0]
        global_cond["mask"] = torch.ones(
            [CANVAS_SIZE, CANVAS_SIZE], dtype=torch.float32
        )

        canvas_state = torch.zeros([CANVAS_SIZE, CANVAS_SIZE], dtype=torch.float32)
        for canvas_cond in canvas_conds[1:][::-1]:
            a, b, c, d = canvas_cond["rect"]
            mask = torch.zeros([CANVAS_SIZE, CANVAS_SIZE], dtype=torch.float32)
            mask[a:b, c:d] = 1.0
            mask = mask * (1 - canvas_state)
            canvas_state += mask
            canvas_cond["mask"] = mask

        return canvas_conds

    def layout_cond(
        self,
        canvas_conds: list[OmostCanvasCondition],
        clip: CLIP,
        global_strength: float,
        region_strength: float,
        positive: ComfyUIConditioning | None = None,
    ):
        """Layout conditioning"""
        positive: ComfyUIConditioning = positive or []
        positive = positive.copy()
        canvas_conds = OmostLayoutCondNode.calc_cond_mask(canvas_conds)

        for i, canvas_cond in enumerate(canvas_conds):
            is_global = i == 0

            prefixes = canvas_cond["prefixes"]
            # Skip the global prefix for region prompts.
            if not is_global:
                prefixes = prefixes[1:]

            cond: ComfyUIConditioning = self.encode_bag_of_subprompts(
                clip, prefixes, canvas_cond["suffixes"]
            )
            # Set area cond
            cond: ComfyUIConditioning = self.cond_set_mask_node.append(
                cond,
                mask=canvas_cond["mask"],
                set_cond_area="default",
                strength=global_strength if is_global else region_strength,
            )[0]
            assert len(cond) == 1
            positive.extend(cond)

        return (positive,)


class OmostLoadCanvasConditioningNode:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "json_str": ("STRING", {"multiline": True}),
            }
        }

    RETURN_TYPES = ("OMOST_CANVAS_CONDITIONING",)
    FUNCTION = "load_canvas"

    def load_canvas(self, json_str: str) -> Tuple[list[OmostCanvasCondition]]:
        """Load canvas from file"""
        return (json.loads(json_str),)


NODE_CLASS_MAPPINGS = {
    "OmostLLMLoaderNode": OmostLLMLoaderNode,
    "OmostLLMChatNode": OmostLLMChatNode,
    "OmostLayoutCondNode": OmostLayoutCondNode,
    "OmostLoadCanvasConditioningNode": OmostLoadCanvasConditioningNode,
    "OmostRenderCanvasConditioningNode": OmostRenderCanvasConditioningNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "OmostLLMLoaderNode": "Omost LLM Loader",
    "OmostLLMChatNode": "Omost LLM Chat",
    "OmostLayoutCondNode": "Omost Layout Cond (ComfyUI-Area)",
    "OmostLoadCanvasConditioningNode": "Omost Load Canvas Conditioning",
    "OmostRenderCanvasConditioningNode": "Omost Render Canvas Conditioning",
}
