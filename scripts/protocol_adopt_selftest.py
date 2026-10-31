# -*- coding: utf-8 -*-
"""自检 / self-check for protocol_adopt 的目标路径翻译：`python3 scripts/protocol_adopt_selftest.py`.

覆盖三种宿主下最容易出错的一件事 / the one thing that breaks per host: 用户填的目标路径
会不会被翻译到本机并不存在的地方。

1. Windows 原生 Python：`D:\\项目` 与 UNC 写法本身就是本地路径，必须原样使用，
   不能变成 `/mnt/d/项目`；
2. WSL：`D:\\项目` → `/mnt/d/项目`，`\\\\wsl.localhost\\...` → `/home/...`；
3. 其他 Linux：Windows 写法无法翻译，必须**明确报错**，不能编一个 `/mnt/d/...`
   然后在错误位置创建文件。

退出码 0 表示全部通过。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import protocol_adopt as adopt  # noqa: E402

FAILURES: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print("  ok   " + label)
        return
    FAILURES.append(label)
    print("  FAIL " + label + " " + detail)


class Host:
    """临时假装自己在某种宿主上 / pretend to be another host for one case."""

    def __init__(self, windows: bool, wsl: bool) -> None:
        self.windows, self.wsl = windows, wsl

    def __enter__(self):
        self._host, self._wsl = adopt.host_is_windows, adopt.in_wsl
        adopt.host_is_windows = lambda: self.windows
        adopt.in_wsl = lambda: self.wsl
        return self

    def __exit__(self, *exc) -> None:
        adopt.host_is_windows, adopt.in_wsl = self._host, self._wsl


def translate(value: str) -> str:
    return str(adopt.translate_target(value))


def run() -> int:
    print("protocol_adopt: target path translation")

    with Host(windows=True, wsl=False):
        check("windows: a drive path stays as written",
              translate("D:\\投资工作站") == "D:\\投资工作站", translate("D:\\投资工作站"))
        check("windows: a forward-slash drive path stays as written",
              translate("D:/投资工作站") == "D:/投资工作站", translate("D:/投资工作站"))
        unc = "\\\\wsl.localhost\\Ubuntu\\home\\example\\p"
        check("windows: an UNC path stays as written", translate(unc) == unc, translate(unc))
        check("windows: a relative path is untouched", translate("my-project") == "my-project",
              translate("my-project"))

    with Host(windows=False, wsl=True):
        check("wsl: a drive path becomes /mnt/<letter>",
              translate("D:\\投资工作站") == "/mnt/d/投资工作站", translate("D:\\投资工作站"))
        check("wsl: a forward-slash drive path becomes /mnt/<letter>",
              translate("D:/投资工作站") == "/mnt/d/投资工作站", translate("D:/投资工作站"))
        check("wsl: an UNC path becomes the real posix path",
              translate(unc) == "/home/example/p", translate(unc))
        check("wsl: a relative path is untouched", translate("my-project") == "my-project",
              translate("my-project"))

    with Host(windows=False, wsl=False):
        try:
            translate("D:\\投资工作站")
        except adopt.TargetPathError as error:
            check("plain linux: a drive path is rejected with an explanation",
                  "不在 WSL" in str(error) and "Windows 版 Python" in str(error), str(error))
        else:
            check("plain linux: a drive path is rejected with an explanation", False, "no error raised")
        check("plain linux: a posix absolute path is untouched",
              translate("/home/example/projects/my-project") == "/home/example/projects/my-project",
              translate("/home/example/projects/my-project"))

    # 真入口跑一次：本机（WSL）里 `--target 'D:\invest-demo' --dry-run` 必须给出计划而不是报错。
    completed = subprocess.run(
        [sys.executable, str(HERE / "protocol_adopt.py"), "--target", "D:\\invest-demo", "--dry-run"],
        cwd=str(HERE.parent), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
    )
    out = completed.stdout.decode("utf-8", "replace")
    err = completed.stderr.decode("utf-8", "replace")
    wsl_host = adopt.in_wsl()
    if wsl_host:
        check("the real entry point plans a translated drive path",
              completed.returncode == 0 and "/mnt/d/invest-demo" in out, (err or out)[-300:])
    else:
        check("the real entry point refuses a drive path outside WSL",
              completed.returncode == 2 and "不在 WSL" in err, (err or out)[-300:])

    print("")
    if FAILURES:
        print("FAILED: %d check(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
