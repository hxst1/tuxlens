# Bundled model

`selfie_segmenter_landscape.onnx` — MediaPipe Selfie Segmentation (landscape, 144×256),
by Google, licensed under the **Apache License 2.0**.

* Original model and model card: https://ai.google.dev/edge/mediapipe/solutions/vision/image_segmenter
* ONNX conversion: https://huggingface.co/onnx-community/mediapipe_selfie_segmentation_landscape (Apache-2.0)
* SHA-256: `7a0adcfdb1715d3b0ff0f61486d8a181c33f4a208343a5abfccbc6540e872d24`

Input `pixel_values` float32 [N, 3, 144, 256] RGB in 0..1; output `alphas` float32 [N, 1, 144, 256].
