# build_app.py
import os
import sys
import shutil
from pathlib import Path
import subprocess
import time

def main():
    project_dir = Path(__file__).parent.absolute()
    exe_path = project_dir / "dist" / "GMS_Test_Launcher.exe"

    if exe_path.exists():
        try:
            subprocess.run(['taskkill', '/f', '/im', 'GMS_Test_Launcher.exe'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            print("🔄 正在终止旧程序...")
            time.sleep(1)
        except Exception as e:
            print(f"⚠️ 关闭程序失败: {e}")

    for folder in ["dist", "build"]:
        if (project_dir / folder).exists():
            shutil.rmtree(project_dir / folder)

    data_args = [
        f"--add-data=run_Device_Lock.sh;.",
        f"--add-data=run_GMS_Test_Auto.sh;.",
        f"--add-data=run_GSI_Burn.sh;.",
        f"--add-data=misc.img;.",
        f"--add-data=upgrade_tool;.",
        f"--add-data=scrcpy-linux-x86_64-v3.3.4.tar.gz;.",
    ]

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--name=GMS_Test_Launcher",
        "--icon=app_icon.ico",
        "--add-data=config.json;.",
        "GMS_Auto_Test_GUI.py"
    ] + data_args

    if not (project_dir / "app_icon.ico").exists():
        print("⚠️ 未找到app_icon.ico，将使用默认图标")
        try:
            cmd.remove("--icon=app_icon.ico")
        except ValueError:
            pass

    print("🚀 开始打包...")
    os.system(" ".join(cmd))

    print(f"\n✅ 打包完成！")
    print(f"📦 可执行文件位置: {project_dir / 'dist' / 'GMS_Test_Launcher.exe'}")
    if exe_path.exists():
        print(f"✅ 正在启动新程序...")
        try:
            subprocess.Popen(str(exe_path), shell=True)
            print(f"🎉 成功启动: {exe_path}")
        except Exception as e:
            print(f"❌ 启动失败: {e}")
    else:
        print(f"❌ 错误：未生成预期文件: {exe_path}")

if __name__ == "__main__":
    main()
