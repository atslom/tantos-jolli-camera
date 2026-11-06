# Tantos Jolli — интеграция Home Assistant (живое видео)

English: [README.md](README.md)

Живое видео с видеопанелей и камер **Tantos Jolli** (XiongMai/Sofia) по протоколу GLNK
как `camera`-сущности Home Assistant. Настраивается через UI (config flow): IP хаба, GID
устройства, логин/пароль. Ничего не захардкожено — все данные вводятся при настройке.

## Установка через HACS
1. Откройте **HACS → Интеграции**.
2. Вверху справа **⋮ → Пользовательские репозитории (Custom repositories)**.
3. Репозиторий: `https://github.com/atslom/tantos-jolli-camera`, категория **Integration**. Нажмите **Добавить**.
4. Найдите **Tantos Jolli** в списке HACS → **Скачать (Download)**.
5. **Перезапустите Home Assistant**.
6. **Настройки → Устройства и службы → Добавить интеграцию → Tantos Jolli**, введите параметры хаба.

## Установка вручную
Скопируйте папку `custom_components/tantos_jolly/` в `/config/custom_components/` на хосте Home Assistant и перезапустите HA.

> Домен интеграции — `tantos_jolly` (исторический идентификатор); устройство называется
> **Tantos Jolli**. Это **интеграция** для HACS — архив SD-записей это отдельный **add-on**
> (github.com/atslom/tantos-ha-addons).
