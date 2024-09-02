import unittest

import numpy as np
import torch
from PIL import Image
from transformers import CLIPTextConfig, CLIPTextModel, CLIPTokenizer

import diffusers
from diffusers import (
    AnimateDiffControlNetPipeline,
    AutoencoderKL,
    ControlNetModel,
    DDIMScheduler,
    DPMSolverMultistepScheduler,
    LCMScheduler,
    MotionAdapter,
    StableDiffusionPipeline,
    UNet2DConditionModel,
    UNetMotionModel,
)
from diffusers.models.attention import FreeNoiseTransformerBlock
from diffusers.utils import logging
from diffusers.utils.import_utils import is_xformers_available
from diffusers.utils.testing_utils import torch_device

from ..pipeline_params import TEXT_TO_IMAGE_BATCH_PARAMS, TEXT_TO_IMAGE_PARAMS
from ..test_pipelines_common import (
    IPAdapterTesterMixin,
    PipelineFromPipeTesterMixin,
    PipelineTesterMixin,
    SDFunctionTesterMixin,
)


def to_np(tensor):
    if isinstance(tensor, torch.Tensor):
        tensor = tensor.detach().cpu().numpy()

    return tensor


class AnimateDiffControlNetPipelineFastTests(
    IPAdapterTesterMixin, SDFunctionTesterMixin, PipelineTesterMixin, PipelineFromPipeTesterMixin, unittest.TestCase
):
    pipeline_class = AnimateDiffControlNetPipeline
    params = TEXT_TO_IMAGE_PARAMS
    batch_params = TEXT_TO_IMAGE_BATCH_PARAMS.union({"conditioning_frames"})
    required_optional_params = frozenset(
        [
            "num_inference_steps",
            "generator",
            "latents",
            "return_dict",
            "callback_on_step_end",
            "callback_on_step_end_tensor_inputs",
        ]
    )

    def get_dummy_components(self):
        cross_attention_dim = 8
        block_out_channels = (8, 8)

        torch.manual_seed(0)
        unet = UNet2DConditionModel(
            block_out_channels=block_out_channels,
            layers_per_block=2,
            sample_size=8,
            in_channels=4,
            out_channels=4,
            down_block_types=("CrossAttnDownBlock2D", "DownBlock2D"),
            up_block_types=("CrossAttnUpBlock2D", "UpBlock2D"),
            cross_attention_dim=cross_attention_dim,
            norm_num_groups=2,
        )
        scheduler = DDIMScheduler(
            beta_start=0.00085,
            beta_end=0.012,
            beta_schedule="linear",
            clip_sample=False,
        )
        torch.manual_seed(0)
        controlnet = ControlNetModel(
            block_out_channels=block_out_channels,
            layers_per_block=2,
            in_channels=4,
            down_block_types=("CrossAttnDownBlock2D", "DownBlock2D"),
            cross_attention_dim=cross_attention_dim,
            conditioning_embedding_out_channels=(8, 8),
            norm_num_groups=1,
        )
        torch.manual_seed(0)
        vae = AutoencoderKL(
            block_out_channels=block_out_channels,
            in_channels=3,
            out_channels=3,
            down_block_types=["DownEncoderBlock2D", "DownEncoderBlock2D"],
            up_block_types=["UpDecoderBlock2D", "UpDecoderBlock2D"],
            latent_channels=4,
            norm_num_groups=2,
        )
        torch.manual_seed(0)
        text_encoder_config = CLIPTextConfig(
            bos_token_id=0,
            eos_token_id=2,
            hidden_size=cross_attention_dim,
            intermediate_size=37,
            layer_norm_eps=1e-05,
            num_attention_heads=4,
            num_hidden_layers=5,
            pad_token_id=1,
            vocab_size=1000,
        )
        text_encoder = CLIPTextModel(text_encoder_config)
        tokenizer = CLIPTokenizer.from_pretrained("hf-internal-testing/tiny-random-clip")
        motion_adapter = MotionAdapter(
            block_out_channels=block_out_channels,
            motion_layers_per_block=2,
            motion_norm_num_groups=2,
            motion_num_attention_heads=4,
        )

        components = {
            "unet": unet,
            "controlnet": controlnet,
            "scheduler": scheduler,
            "vae": vae,
            "motion_adapter": motion_adapter,
            "text_encoder": text_encoder,
            "tokenizer": tokenizer,
            "feature_extractor": None,
            "image_encoder": None,
        }
        return components

    def get_dummy_inputs(self, device, seed: int = 0, num_frames: int = 2):
        if str(device).startswith("mps"):
            generator = torch.manual_seed(seed)
        else:
            generator = torch.Generator(device=device).manual_seed(seed)

        video_height = 32
        video_width = 32
        conditioning_frames = [Image.new("RGB", (video_width, video_height))] * num_frames

        inputs = {
            "prompt": "A painting of a squirrel eating a burger",
            "conditioning_frames": conditioning_frames,
            "generator": generator,
            "num_inference_steps": 2,
            "num_frames": num_frames,
            "guidance_scale": 7.5,
            "output_type": "pt",
        }
        return inputs

    def test_from_pipe_consistent_config(self):
        assert self.original_pipeline_class == StableDiffusionPipeline
        original_repo = "hf-internal-testing/tinier-stable-diffusion-pipe"
        original_kwargs = {"requires_safety_checker": False}

        # create original_pipeline_class(sd)
        pipe_original = self.original_pipeline_class.from_pretrained(original_repo, **original_kwargs)

        # original_pipeline_class(sd) -> pipeline_class
        pipe_components = self.get_dummy_components()
        pipe_additional_components = {}
        for name, component in pipe_components.items():
            if name not in pipe_original.components:
                pipe_additional_components[name] = component

        pipe = self.pipeline_class.from_pipe(pipe_original, **pipe_additional_components)

        # pipeline_class -> original_pipeline_class(sd)
        original_pipe_additional_components = {}
        for name, component in pipe_original.components.items():
            if name not in pipe.components or not isinstance(component, pipe.components[name].__class__):
                original_pipe_additional_components[name] = component

        pipe_original_2 = self.original_pipeline_class.from_pipe(pipe, **original_pipe_additional_components)

        # compare the config
        original_config = {k: v for k, v in pipe_original.config.items() if not k.startswith("_")}
        original_config_2 = {k: v for k, v in pipe_original_2.config.items() if not k.startswith("_")}
        assert original_config_2 == original_config

    def test_motion_unet_loading(self):
        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)

        assert isinstance(pipe.unet, UNetMotionModel)

    @unittest.skip("Attention slicing is not enabled in this pipeline")
    def test_attention_slicing_forward_pass(self):
        pass

    def test_ip_adapter_single(self):
        expected_pipe_slice = None
        if torch_device == "cpu":
            expected_pipe_slice = np.array(
                [
                    0.6604,
                    0.4099,
                    0.4928,
                    0.5706,
                    0.5096,
                    0.5012,
                    0.6051,
                    0.5169,
                    0.5021,
                    0.4864,
                    0.4261,
                    0.5779,
                    0.5822,
                    0.4049,
                    0.5253,
                    0.6160,
                    0.4150,
                    0.5155,
                ]
            )
        return super().test_ip_adapter_single(expected_pipe_slice=expected_pipe_slice)

    def test_dict_tuple_outputs_equivalent(self):
        expected_slice = None
        if torch_device == "cpu":
            expected_slice = np.array([0.6051, 0.5169, 0.5021, 0.6160, 0.4150, 0.5155])
        return super().test_dict_tuple_outputs_equivalent(expected_slice=expected_slice)

    def test_inference_batch_single_identical(
        self,
        batch_size=2,
        expected_max_diff=1e-4,
        additional_params_copy_to_batched_inputs=["num_inference_steps"],
    ):
        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)
        for components in pipe.components.values():
            if hasattr(components, "set_default_attn_processor"):
                components.set_default_attn_processor()

        pipe.to(torch_device)
        pipe.set_progress_bar_config(disable=None)
        inputs = self.get_dummy_inputs(torch_device)
        # Reset generator in case it is has been used in self.get_dummy_inputs
        inputs["generator"] = self.get_generator(0)

        logger = logging.get_logger(pipe.__module__)
        logger.setLevel(level=diffusers.logging.FATAL)

        # batchify inputs
        batched_inputs = {}
        batched_inputs.update(inputs)

        for name in self.batch_params:
            if name not in inputs:
                continue

            value = inputs[name]
            if name == "prompt":
                len_prompt = len(value)
                batched_inputs[name] = [value[: len_prompt // i] for i in range(1, batch_size + 1)]
                batched_inputs[name][-1] = 100 * "very long"

            else:
                batched_inputs[name] = batch_size * [value]

        if "generator" in inputs:
            batched_inputs["generator"] = [self.get_generator(i) for i in range(batch_size)]

        if "batch_size" in inputs:
            batched_inputs["batch_size"] = batch_size

        for arg in additional_params_copy_to_batched_inputs:
            batched_inputs[arg] = inputs[arg]

        output = pipe(**inputs)
        output_batch = pipe(**batched_inputs)

        assert output_batch[0].shape[0] == batch_size

        max_diff = np.abs(to_np(output_batch[0][0]) - to_np(output[0][0])).max()
        assert max_diff < expected_max_diff

    @unittest.skipIf(torch_device != "cuda", reason="CUDA and CPU are required to switch devices")
    def test_to_device(self):
        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)
        pipe.set_progress_bar_config(disable=None)

        pipe.to("cpu")
        # pipeline creates a new motion UNet under the hood. So we need to check the device from pipe.components
        model_devices = [
            component.device.type for component in pipe.components.values() if hasattr(component, "device")
        ]
        self.assertTrue(all(device == "cpu" for device in model_devices))

        output_cpu = pipe(**self.get_dummy_inputs("cpu"))[0]
        self.assertTrue(np.isnan(output_cpu).sum() == 0)

        pipe.to("cuda")
        model_devices = [
            component.device.type for component in pipe.components.values() if hasattr(component, "device")
        ]
        self.assertTrue(all(device == "cuda" for device in model_devices))

        output_cuda = pipe(**self.get_dummy_inputs("cuda"))[0]
        self.assertTrue(np.isnan(to_np(output_cuda)).sum() == 0)

    def test_to_dtype(self):
        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)
        pipe.set_progress_bar_config(disable=None)

        # pipeline creates a new motion UNet under the hood. So we need to check the dtype from pipe.components
        model_dtypes = [component.dtype for component in pipe.components.values() if hasattr(component, "dtype")]
        self.assertTrue(all(dtype == torch.float32 for dtype in model_dtypes))

        pipe.to(dtype=torch.float16)
        model_dtypes = [component.dtype for component in pipe.components.values() if hasattr(component, "dtype")]
        self.assertTrue(all(dtype == torch.float16 for dtype in model_dtypes))

    def test_prompt_embeds(self):
        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)
        pipe.set_progress_bar_config(disable=None)
        pipe.to(torch_device)

        inputs = self.get_dummy_inputs(torch_device)
        inputs.pop("prompt")
        inputs["prompt_embeds"] = torch.randn((1, 4, pipe.text_encoder.config.hidden_size), device=torch_device)
        pipe(**inputs)

    @unittest.skipIf(
        torch_device != "cuda" or not is_xformers_available(),
        reason="XFormers attention is only available with CUDA and `xformers` installed",
    )
    def test_xformers_attention_forwardGenerator_pass(self):
        super()._test_xformers_attention_forwardGenerator_pass(test_mean_pixel_difference=False)

    def test_free_init(self):
        components = self.get_dummy_components()
        pipe: AnimateDiffControlNetPipeline = self.pipeline_class(**components)
        pipe.set_progress_bar_config(disable=None)
        pipe.to(torch_device)

        inputs_normal = self.get_dummy_inputs(torch_device)
        frames_normal = pipe(**inputs_normal).frames[0]

        pipe.enable_free_init(
            num_iters=2,
            use_fast_sampling=True,
            method="butterworth",
            order=4,
            spatial_stop_frequency=0.25,
            temporal_stop_frequency=0.25,
        )
        inputs_enable_free_init = self.get_dummy_inputs(torch_device)
        frames_enable_free_init = pipe(**inputs_enable_free_init).frames[0]

        pipe.disable_free_init()
        inputs_disable_free_init = self.get_dummy_inputs(torch_device)
        frames_disable_free_init = pipe(**inputs_disable_free_init).frames[0]

        sum_enabled = np.abs(to_np(frames_normal) - to_np(frames_enable_free_init)).sum()
        max_diff_disabled = np.abs(to_np(frames_normal) - to_np(frames_disable_free_init)).max()
        self.assertGreater(
            sum_enabled, 1e1, "Enabling of FreeInit should lead to results different from the default pipeline results"
        )
        self.assertLess(
            max_diff_disabled,
            1e-4,
            "Disabling of FreeInit should lead to results similar to the default pipeline results",
        )

    def test_free_init_with_schedulers(self):
        components = self.get_dummy_components()
        pipe: AnimateDiffControlNetPipeline = self.pipeline_class(**components)
        pipe.set_progress_bar_config(disable=None)
        pipe.to(torch_device)

        inputs_normal = self.get_dummy_inputs(torch_device)
        frames_normal = pipe(**inputs_normal).frames[0]

        schedulers_to_test = [
            DPMSolverMultistepScheduler.from_config(
                components["scheduler"].config,
                timestep_spacing="linspace",
                beta_schedule="linear",
                algorithm_type="dpmsolver++",
                steps_offset=1,
                clip_sample=False,
            ),
            LCMScheduler.from_config(
                components["scheduler"].config,
                timestep_spacing="linspace",
                beta_schedule="linear",
                steps_offset=1,
                clip_sample=False,
            ),
        ]
        components.pop("scheduler")

        for scheduler in schedulers_to_test:
            components["scheduler"] = scheduler
            pipe: AnimateDiffControlNetPipeline = self.pipeline_class(**components)
            pipe.set_progress_bar_config(disable=None)
            pipe.to(torch_device)

            pipe.enable_free_init(num_iters=2, use_fast_sampling=False)

            inputs = self.get_dummy_inputs(torch_device)
            frames_enable_free_init = pipe(**inputs).frames[0]
            sum_enabled = np.abs(to_np(frames_normal) - to_np(frames_enable_free_init)).sum()

            self.assertGreater(
                sum_enabled,
                1e1,
                "Enabling of FreeInit should lead to results different from the default pipeline results",
            )

    def test_free_noise_blocks(self):
        components = self.get_dummy_components()
        pipe: AnimateDiffControlNetPipeline = self.pipeline_class(**components)
        pipe.set_progress_bar_config(disable=None)
        pipe.to(torch_device)

        pipe.enable_free_noise()
        for block in pipe.unet.down_blocks:
            for motion_module in block.motion_modules:
                for transformer_block in motion_module.transformer_blocks:
                    self.assertTrue(
                        isinstance(transformer_block, FreeNoiseTransformerBlock),
                        "Motion module transformer blocks must be an instance of `FreeNoiseTransformerBlock` after enabling FreeNoise.",
                    )

        pipe.disable_free_noise()
        for block in pipe.unet.down_blocks:
            for motion_module in block.motion_modules:
                for transformer_block in motion_module.transformer_blocks:
                    self.assertFalse(
                        isinstance(transformer_block, FreeNoiseTransformerBlock),
                        "Motion module transformer blocks must not be an instance of `FreeNoiseTransformerBlock` after disabling FreeNoise.",
                    )

    def test_free_noise(self):
        components = self.get_dummy_components()
        pipe: AnimateDiffControlNetPipeline = self.pipeline_class(**components)
        pipe.set_progress_bar_config(disable=None)
        pipe.to(torch_device)

        inputs_normal = self.get_dummy_inputs(torch_device, num_frames=16)
        frames_normal = pipe(**inputs_normal).frames[0]

        for context_length in [8, 9]:
            for context_stride in [4, 6]:
                pipe.enable_free_noise(context_length, context_stride)

                inputs_enable_free_noise = self.get_dummy_inputs(torch_device, num_frames=16)
                frames_enable_free_noise = pipe(**inputs_enable_free_noise).frames[0]

                pipe.disable_free_noise()

                inputs_disable_free_noise = self.get_dummy_inputs(torch_device, num_frames=16)
                frames_disable_free_noise = pipe(**inputs_disable_free_noise).frames[0]

                sum_enabled = np.abs(to_np(frames_normal) - to_np(frames_enable_free_noise)).sum()
                max_diff_disabled = np.abs(to_np(frames_normal) - to_np(frames_disable_free_noise)).max()
                self.assertGreater(
                    sum_enabled,
                    1e1,
                    "Enabling of FreeNoise should lead to results different from the default pipeline results",
                )
                self.assertLess(
                    max_diff_disabled,
                    1e-4,
                    "Disabling of FreeNoise should lead to results similar to the default pipeline results",
                )

    def test_free_noise_multi_prompt(self):
        components = self.get_dummy_components()
        pipe: AnimateDiffControlNetPipeline = self.pipeline_class(**components)
        pipe.set_progress_bar_config(disable=None)
        pipe.to(torch_device)

        context_length = 8
        context_stride = 4
        pipe.enable_free_noise(context_length, context_stride)

        # Make sure that pipeline works when prompt indices are within num_frames bounds
        inputs = self.get_dummy_inputs(torch_device, num_frames=16)
        inputs["prompt"] = {0: "Caterpillar on a leaf", 10: "Butterfly on a leaf"}
        pipe(**inputs).frames[0]

        with self.assertRaises(ValueError):
            # Ensure that prompt indices are within bounds
            inputs = self.get_dummy_inputs(torch_device, num_frames=16)
            inputs["prompt"] = {0: "Caterpillar on a leaf", 10: "Butterfly on a leaf", 42: "Error on a leaf"}
            pipe(**inputs).frames[0]

    def test_vae_slicing(self, video_count=2):
        device = "cpu"  # ensure determinism for the device-dependent torch.Generator
        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)
        pipe = pipe.to(device)
        pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs(device)
        inputs["prompt"] = [inputs["prompt"]] * video_count
        inputs["conditioning_frames"] = [inputs["conditioning_frames"]] * video_count
        output_1 = pipe(**inputs)

        # make sure sliced vae decode yields the same result
        pipe.enable_vae_slicing()
        inputs = self.get_dummy_inputs(device)
        inputs["prompt"] = [inputs["prompt"]] * video_count
        inputs["conditioning_frames"] = [inputs["conditioning_frames"]] * video_count
        output_2 = pipe(**inputs)

        assert np.abs(output_2[0].flatten() - output_1[0].flatten()).max() < 1e-2
