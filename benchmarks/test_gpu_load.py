"""Test if Granite Guardian 5B loads on GPU."""
import torch
from transformers import AutoModelForCausalLM

print(f"CUDA: {torch.cuda.is_available()}")
print(f"VRAM free: {torch.cuda.mem_get_info(0)[0]/1e9:.1f} GB")

print("Loading to GPU with device_map={'': 0}...")
m = AutoModelForCausalLM.from_pretrained(
    "ibm-granite/granite-guardian-3.2-5b",
    device_map={"": 0},
    torch_dtype=torch.bfloat16,
)
print(f"Device: {next(m.parameters()).device}")
print(f"VRAM used: {torch.cuda.memory_allocated(0)/1e9:.1f} GB")
del m
torch.cuda.empty_cache()
print("OK - GPU load works")
