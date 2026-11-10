# LittleFS Programmer

Windows tool to build a LittleFS image (`littlefs.bin`) from a `data` folder, verify it, and flash it to an ESP32.
ابزار ویندوزی برای ساخت فایل `littlefs.bin` از پوشه‌ی `data`، بررسی آن و برنامه‌ریزی روی ESP32.

- Build with `mklittlefs` (block 4096, page 256) and verify by unpacking and comparing every file (SHA-256)
- Auto or manual device detection (COM port, chip, flash size, partition table)
- Flash with `esptool` and verify the written data by hash
- Persian (RTL) and English (LTR) interface, dark theme, sound alerts

## Run from source
```
pip install esptool pyserial pillow pyinstaller
python LittleFS_Programmer.py
```

## Build the exe
```
python -m PyInstaller --noconfirm --clean --onefile --noconsole --name LittleFS_Programmer ^
  --icon assets/app.ico --add-data "assets;assets" --collect-all esptool --collect-all espsecure ^
  --collect-all espefuse --hidden-import serial.tools.list_ports LittleFS_Programmer.py
```

Contact: [WWW.IOTHERO.IR](https://www.iothero.ir)
