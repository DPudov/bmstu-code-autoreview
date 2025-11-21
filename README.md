# Авторевьюшница

Ревьювит код по правилам и ставит апрув, если ей все нравится. МР должен быть назначен ASSIGNEE, по умолчанию на владельца токена

# Настройка перед запуском

1. Установи python (желательно 3.13)

2. Выполни следующую команду
```bash
pip3 install -r requirements.txt
```
3. Укажи приватный gitlab token в default.conf в GITLAB_TOKEN. Поменяй название файла на script.conf

Токен должен иметь права api, read_user, read_repository, write_repository (лучше указать все)

Также можно указать все требуемые настройки

# Запуск

```bash
python3 auto_review.py
```
