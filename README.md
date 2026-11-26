# MSSQL Backup

Пакет Synology DSM для резервного копирования баз Microsoft SQL Server на NAS.

DSM 7.2 и новее, в том числе DS425+ (DSM 7.4). Интерфейс открывается окном рабочего стола DSM.

## Установка

1. Скачайте `MSSQLBackup-1.14.0.spk` из [релизов](https://github.com/GrafAtRuss/synology-mssql-backup/releases).
2. Центр пакетов → Установка вручную.
3. Запустите пакет. В мастере: подключение к SQL Server, выбор баз, папка на сервере и SMB-шара, папка на NAS, расписание, ротация.

SQL Server пишет `.bak` в локальную папку (например `C:\SQLBackups`). NAS забирает файл по SMB. UNC в команде `BACKUP` не используется.

`sqlcmd` входит в пакет (linux-amd64 и linux-arm64). Отдельно ставить его не нужно. Расписание выполняет служба пакета, планировщик DSM не требуется.

Пользователь пакета `MSSQLBackup` должен иметь право записи в выбранную общую папку.

## Сборка

Каталог пакета: `INFO`, `PACKAGE_ICON*.PNG`, `conf/`, `scripts/`, `ui/`, `bin/`.

```sh
tar -czf package.tgz bin ui
tar -cf MSSQLBackup.spk INFO package.tgz scripts conf PACKAGE_ICON.PNG PACKAGE_ICON_256.PNG
```

## Автор

Ермаков Алексей Сергеевич, ИП Ермаков А.С.  
https://ип-ермаков.рф · info@ip-ermakov.ru · +7-991-866-1848
