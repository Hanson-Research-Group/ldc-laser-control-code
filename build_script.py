import os
import sys

def build_exe():
    print("Starting build process...")

    # Build options. The GUI is PySide6/Qt; PyInstaller's bundled PySide6 hook
    # collects the required Qt plugins automatically, so no --add-data is needed
    # for the framework itself — only our icon resources.
    params = [
        'src/main.py',
        '--onefile',
        '--noconsole',
        '--name=LaserControllerConsole',
        f'--add-data=src/laser_controller_icon.png{os.pathsep}src/',
        f'--add-data=src/laser_controller_icon.ico{os.pathsep}src/',
        '--icon=src/laser_controller_icon.ico'
    ]

    print(f"Running PyInstaller with arguments: {params}")
    
    # Run PyInstaller programmatically
    import PyInstaller.__main__
    PyInstaller.__main__.run(params)

    print("PyInstaller build finished.")
    
    # Verify binary exists
    exe_name = "LaserControllerConsole.exe"
    exe_path = os.path.join("dist", exe_name)
    if os.path.exists(exe_path):
        print(f"Success! Executable built at: {os.path.abspath(exe_path)}")
    else:
        print("Error: Executable not found in dist folder.", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    build_exe()
