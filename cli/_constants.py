"""Shared CLI constants (argument choices)."""

VALID_MEDIA_BUNDLES = ("image_generation", "z_image", "image_editing", "i2v")
VALID_I2V_DURATIONS = ("5s", "8s")
VALID_I2V_ASPECT_RATIOS = ("2:3", "3:2", "1:1")

# The built-in TEXT engines `grid join --serve` can launch, selected with `--engine`. llama.cpp is
# the default and runs a .gguf file; mlx-omarchy runs an MLX-format model on an Apple Silicon Mac
# booted into Linux (see `shared/engine/mlx_omarchy.py`). ComfyUI is not here: it is `--media`.
BUILTIN_TEXT_ENGINES = ("llama.cpp", "mlx-omarchy")
DEFAULT_TEXT_ENGINE = "llama.cpp"
