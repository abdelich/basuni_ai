# Статус: бот «Старейшина» и дальнейшие шаги

## Что имеем сейчас

### Оркестратор и инфраструктура
- **main.py** — точка входа, запуск оркестратора.
- **Конфиг**: `.env` (токены, `OPENAI_API_KEY`, `DATABASE_URL`) + `config/default.yaml` (guild_id, channels, role_ids, enabled_roles, привязка каналов по ролям).
- **БД**: SQLAlchemy async; у каждого бота своя локальная таблица. У старейшин — `elder_cases` (id, guild_id, case_type, status, author_id, channel_id, thread_id, initial_content, meta, elder_decided_at, elder_decision, elder_already_decided и т.д.). Создание таблиц при старте.
- **Роли**: реестр в `src/roles/`, базовая роль `RoleBot`, старейшина зарегистрирована и запускается по `enabled_roles`.

### Агент и инструменты
- **Agent** (`src/core/agent.py`): вызов LLM (OpenAI) с function calling, цикл «ответ → tool_calls → выполнение → повтор» до финального текста.
- **Tool** (`src/core/tools.py`): контракт инструмента (name, description, parameters, execute), преобразование в формат OpenAI.
- **AgentContext**: guild_id, channel_ids, bot, db_session_factory — передаётся в инструменты.

### Привязка каналов
- **config.channel_for_role(role_key, purpose)** — возвращает ID канала по назначению: `inbox`, `decisions`, `outbox`, `notify_court`, `notify_council`, `referrals`.
- В YAML задаются ключи каналов (`elder_inbox`, `elder_decisions`, …) и привязка в `roles.elder.*_channel_key`.

### Бот «Старейшина»
- **ElderBot** (`src/roles/elder/bot.py`):
  - Сообщения в канале-inbox обрабатываются агентом (системный промпт + инструменты).
  - Ответ отправляется в тот же канал.
- **Инструменты** (`src/roles/elder/tools.py`):
  - `send_message` — отправить текст в outbox или decisions.
  - `publish_decision` — опубликовать решение (confirm_process / send_to_council / return_to_court) в канал решений.
  - `notify_channel` — уведомить суд или совет (отправка в court_inbox / council_inbox).
  - `get_case` — получить дело по ID из БД.
  - `list_elder_cases` — список дел у старейшин по статусу.
- **Логика** (`src/roles/elder/logic.py`): типы дел старейшин, допустимые решения, проверки.
- **Промпт** (`prompts/elder_system.md`): системный промпт роли для LLM.

### Сделано для тестирования
- Проверка ПМЖ: только пользователи с ролью `role_ids.pmj` могут обращаться к старейшинам; при `pmj: 0` проверка отключена.
- По каждому сообщению в inbox создаётся запись в `elder_cases`, в агента передаётся номер дела (Обращение №N).
- После `publish_decision` дело закрывается, ставится флаг `elder_already_decided` (повторное решение по тому же делу запрещено).
- README с установкой и шагами тестирования.

### Чего пока нет
- Slash-команды (только обработка текста в канале).
- Автотесты (unit / integration).

---

## Дальнейшие шаги для старейшины и тестирования

### 1. Подготовка к первому запуску (обязательно)
- [ ] Скопировать `.env.example` в `.env`.
- [ ] Создать приложение бота в [Discord Developer Portal](https://discord.com/developers/applications), получить токен, прописать `DISCORD_TOKEN_ELDER` в `.env`.
- [ ] Включить в настройках приложения: Message Content Intent (при необходимости — Server Members Intent).
- [ ] Добавить бота на сервер (OAuth2 → URL с scope `bot`, права по необходимости).
- [ ] Заполнить `config/default.yaml`: `guild_id`, ID каналов (`elder_inbox`, `elder_decisions`, при желании `court_inbox`, `council_inbox`). Остальные можно оставить 0.
- [ ] Задать `OPENAI_API_KEY` в `.env` для работы агента.

### 2. Первый запуск и ручная проверка
- [ ] Из корня проекта: `python main.py` (или через venv: `.\basuni\Scripts\Activate.ps1` и `python main.py`).
- [ ] Убедиться, что бот в сети и видит гильдию.
- [ ] Написать в канал `elder_inbox` любое сообщение (например: «Хочу подать апелляцию по делу 1»).
- [ ] Проверить: бот отвечает в тот же канал (при наличии API ключа — ответ от LLM; без ключа — сообщение об ошибке).

### 3. Доработки старейшины (по приоритету)
- [ ] **Проверка ПМЖ**: перед обработкой сообщения проверять, что у автора есть роль с ID из `config.role_ids()["pmj"]`; иначе отвечать отказом.
- [ ] **Создание дела при обращении**: при первом сообщении в треде (или по команде) создавать запись в `cases` (case_type = appeal_procedure / referendum_request и т.д.), сохранять case_id в контексте/треде.
- [ ] **Slash-команды** (по желанию): например `/appeal`, `/referendum_request`, `/elders_status` — вызов тех же инструментов через команды.
- [ ] **Обновление статуса дела**: после `publish_decision` обновлять запись в БД (status, meta с решением).
- [ ] **Запрет повторного вмешательства**: при возврате дела в суд выставлять флаг «старейшины уже выносили решение» и не позволять повторно рассматривать то же дело.

### 4. БД и данные
- [ ] Расширить модель `Case` при необходимости: author_id, channel_id, thread_id, elder_decided_at, precedent_id и т.д.
- [ ] Добавить таблицы под апелляции и референдумы, если решим хранить их отдельно от `cases.meta`.
- [ ] В инструментах: создание дела (`create_case` / `register_appeal`), обновление статуса после решения.

### 5. Тестирование
- [ ] **Локальный тест агента без Discord**: скрипт, который создаёт Agent с промптом старейшины и инструментами-заглушками (без реальных каналов), передаёт тестовое сообщение и проверяет ответ/вызов инструментов.
- [ ] **Unit-тесты** для `logic.py` (elder_may_consider, elder_may_decide) и для парсинга ответа LLM (РЕШЕНИЕ: …).
- [ ] **Интеграционный тест**: поднятие БД (SQLite in-memory), создание дела, вызов `get_case` / `list_elder_cases` через инструменты.

### 6. Документация и конфиг
- [ ] В README: как установить зависимости, настроить `.env` и `default.yaml`, запустить оркестратор.
- [ ] В комментариях или в ARCHITECTURE: описание потока «сообщение → inbox → агент → инструменты → ответ».

---

## Краткий чек-лист перед тестом «в бою»

1. `.env`: `DISCORD_TOKEN_ELDER`, `OPENAI_API_KEY`.
2. `config/default.yaml`: `guild_id`, `channels.elder_inbox` (и при желании `elder_decisions`).
3. Запуск: `python main.py`.
4. Сообщение в канал с ID `elder_inbox` → ожидание ответа бота в том же канале.

После этого можно поочерёдно вводить проверку ПМЖ, запись дел в БД и slash-команды.
