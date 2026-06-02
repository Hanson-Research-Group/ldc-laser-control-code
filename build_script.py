import os
import sys
import shutil
import subprocess
import customtkinter

def build_exe():
    print("Starting build process...")
    
    # Path of customtkinter
    ctk_path = os.path.dirname(customtkinter.__file__)
    print(f"customtkinter path: {ctk_path}")

    # Build options
    params = [
        'src/main.py',
        '--onefile',
        '--noconsole',
        '--name=LDC3908_ModularLaserDiodeControllerSoftware',
        f'--add-data={ctk_path}{os.pathsep}customtkinter/'
    ]

    print(f"Running PyInstaller with arguments: {params}")
    
    # Run PyInstaller programmatically
    import PyInstaller.__main__
    PyInstaller.__main__.run(params)

    print("PyInstaller build finished.")
    
    # Verify binary exists
    exe_name = "LDC3908_ModularLaserDiodeControllerSoftware.exe"
    exe_path = os.path.join("dist", exe_name)
    if os.path.exists(exe_path):
        print(f"Success! Executable built at: {os.path.abspath(exe_path)}")
    else:
        print("Error: Executable not found in dist folder.", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    build_exe()
