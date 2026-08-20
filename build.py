"""一键打包：onedir + zip（文件名自动带版本号）"""
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

sys.path.insert(0, str(ROOT))
from src.version import VERSION  # noqa: E402

PYI = ROOT / ".venv" / "Scripts" / "pyinstaller.exe"
DIST = ROOT / "dist"
APP_DIR = DIST / "HexMayhemHelper"
ZIP = DIST / f"HexMayhemHelper_v{VERSION}.zip"


def main() -> int:
    release = "--release" in sys.argv or os.environ.get("HMH_RELEASE") == "1"
    # 命名惯例：公开版带平台后缀（仅支持 Windows x64）
    if release:
        zip_name = f"HexMayhemHelper_v{VERSION}-windows-x64.zip"
    else:
        zip_name = f"HexMayhemHelper_v{VERSION}.zip"
    ZIP = DIST / zip_name

    # 1) 清理
    subprocess.run(["taskkill", "/f", "/im", "HexMayhemHelper.exe"],
                   capture_output=True)
    shutil.rmtree(APP_DIR, ignore_errors=True)
    for old in DIST.glob(f"HexMayhemHelper_v{VERSION}*.zip"):
        old.unlink()
    suffix = " (public, 无内置Key)" if release else ""
    print(f"[1/4] version v{VERSION}{suffix}")

    # 2) PyInstaller（onedir）
    # release 模式：临时用无 Key 的 secrets.example.py 覆盖 secrets.py，打包后恢复
    bak = None
    if release:
        src_secrets = ROOT / "src" / "secrets.py"
        bak = src_secrets.with_suffix(".py.bak")
        shutil.copy(src_secrets, bak)
        shutil.copy(ROOT / "src" / "secrets.example.py", src_secrets)
        print("[2/4] release 模式: 已切换无内置 Key 的 secrets")
    try:
        cmd = [
            str(PYI), "--noconfirm", "--windowed", "--name", "HexMayhemHelper",
            "--collect-all", "winrt",
            "--exclude-module", "tkinter", "--exclude-module", "unittest",
            "--exclude-module", "pydoc",
            "main.py",
        ]
        print("[2/4] PyInstaller...")
        r = subprocess.run(cmd)
        if r.returncode != 0:
            print("BUILD_FAILED")
            return 1
    finally:
        if bak is not None and bak.exists():
            shutil.move(str(bak), str(ROOT / "src" / "secrets.py"))
            print("[2/4] 已恢复本地 secrets.py")

    # 3) 生成 api_key.txt 模板（用户可填自己的 Key）
    print("[3/4] api_key.txt...")
    template = (
        "# ============================================ #\n"
        "# 海克斯大乱斗助手 - API Key 配置                #\n"
        "# 在这里填写你自己申请的 aramgg API Key          #\n"
        "# 申请地址: https://data.dtodo.cn/developer.html #\n"
        "# (GitHub 授权登录, 给海克斯助手项目点 Star 即可) #\n"
        "#                                             #\n"
        "# 用法: 把 Key 粘贴到下面空白行, 保存后重启软件   #\n"
        "# 留空或删除本文件 = 使用软件内置 Key            #\n"
        "# ============================================ #\n\n\n"
    )
    (APP_DIR / "api_key.txt").write_text(template, encoding="utf-8")

    # 4) zip
    print("[4/4] compress...")
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in sorted(APP_DIR.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(APP_DIR))
    size_mb = ZIP.stat().st_size / 1048576
    print(f"[4/4] DONE: {ZIP.name} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
