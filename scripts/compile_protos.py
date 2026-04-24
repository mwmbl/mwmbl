#!/usr/bin/env python3
"""Compile Protocol Buffer schemas to Python code"""
import sys
import subprocess
from pathlib import Path


def main():
    """Compile .proto files to Python"""
    # Get project root
    project_root = Path(__file__).parent.parent
    
    proto_files = [
        'mwmbl/site/schemas/structured_content.proto',
    ]
    
    print("Compiling Protocol Buffer schemas...")
    
    for proto_file in proto_files:
        proto_path = project_root / proto_file
        
        if not proto_path.exists():
            print(f"Error: {proto_file} not found")
            sys.exit(1)
        
        print(f"  Compiling {proto_file}...")
        
        result = subprocess.run([
            'python', '-m', 'grpc_tools.protoc',
            f'--proto_path={project_root}',
            f'--python_out={project_root}',
            str(proto_file),
        ], capture_output=True, text=True)
        
        if result.returncode != 0:
            print(f"Error compiling {proto_file}:")
            print(result.stderr)
            sys.exit(1)
    
    print("✓ Successfully compiled all proto files")
    print("\nGenerated files:")
    for proto_file in proto_files:
        py_file = proto_file.replace('.proto', '_pb2.py')
        print(f"  - {py_file}")


if __name__ == '__main__':
    main()
