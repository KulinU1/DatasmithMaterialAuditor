# Bundled Qt for Python

同事电脑安装：同步或复制完整 `DatasmithMaterialAuditor` 插件后重启 UE 5.8，即可打开“Datasmith 场景处理”面板，不需要单独配置 PySide6、shiboken6 或执行 pip。必须保留 `Content/Python/Lib/Win64/site-packages` 下所有文件。这份依赖包适用于 Windows x64 / UE 5.8 内嵌 Python 3.11。

This plugin includes the unmodified Windows x64 wheels from PyPI:

- PySide6 6.11.1
- PySide6_Essentials 6.11.1
- PySide6_Addons 6.11.1
- shiboken6 6.11.1

They are installed under `Content/Python/Lib/Win64/site-packages` for Unreal Engine 5.8's Python 3.11. The package metadata, license texts, Qt DLLs, and Qt platform plugins are retained. Each distribution's license notices are in its `*.dist-info/licenses` directory; additional upstream notices are retained in the package files.

The upstream LGPLv3, GPLv2 and GPLv3 license texts are also provided in `ThirdPartyLicenses`, copied verbatim from Qt's v6.11.1 source tree.

Upstream source and release information:

- https://code.qt.io/cgit/pyside/pyside-setup.git/ (release tag v6.11.1)
- https://download.qt.io/official_releases/QtForPython/
- https://doc.qt.io/qtforpython-6/
- https://pypi.org/project/PySide6/6.11.1/
- https://pypi.org/project/shiboken6/6.11.1/

These libraries retain their upstream licenses. They are shipped as replaceable dynamic libraries; no Qt or PySide source was modified.
