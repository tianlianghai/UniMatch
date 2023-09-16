import subprocess
import sys
import torch

num_gpu = torch.cuda.device_count()
if num_gpu > 1:
    command = [
        "accelerate", "launch", 
        "unimatch.py",
        *sys.argv[1:]  # Pass the command-line arguments except the script name
    ]
else: 
    print("using python instead of accelerate launch")
    command = [
        "python", 
        "unimatch.py",
        *sys.argv[1:]  # Pass the command-line arguments except the script name
    ]

subprocess.run(command)