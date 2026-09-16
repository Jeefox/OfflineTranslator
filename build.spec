# -*- mode: python ; coding: utf-8 -*-
block_cipher = None

a = Analysis(
    ['gui_ctk.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('glossary.txt', '.'),
        ('wordlist.txt', '.'),
    ],
    hiddenimports=[
        'customtkinter',
        'pynput',
        'pynput.keyboard',
        'pynput.mouse',
        'pyperclip',
        'ctranslate2',
        'sentencepiece',
        'pystray',
        'pystray._x11',
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
        'platformdirs',
        'zipfile',
        'tempfile',
        'shutil',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'numpy', 'scipy', 'pandas',
        'stanza', 'torch', 'argostranslate',
        'tkinter.test', 'tkinter.tix',
        'IPython', 'jupyter', 'notebook',
        'Cython', 'setuptools', 'pkg_resources',
        'pip', 'wheel',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
    name='OfflineTranslate',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=['ctranslate2', 'sentencepiece'],
    runtime_tmpdir=None,
    console=False,
)
