# Action Catalog

Дата создания: 2026-09-03. Обновлено 2026-09-07 после реальной разведки CRM/Webitel. Единственный источник правды об исполняемых действиях — `ActionRegistry` в коде (`src/crm_ai_agent/safety/action_registry.py`). Этот документ — человекочитаемая проекция.

## Действия MVP (реализованы в коде)

| action_type | Статус | risk_level | required_approval | Примечание |
|---|---|---|---|---|
| `find_crm_user` | Доступно (read-only) | LOW | Нет | Используется Entity Resolver, не LLM напрямую |
| `edit_crm_user_name` | Доступно — гипотетический MVP-сценарий | MEDIUM | Да | Только `first_name`, `last_name`; требует единственного однозначного совпадения пользователя. Полная цепочка (reader → interpreter → resolver → compiler) реализована и покрыта тестами на fixture-данных |
| `add_ticket_comment` | Internal-only | LOW | Нет (вызывается только finalizer'ом) | LLM не может вызвать напрямую |
| `close_ticket` | Internal-only | LOW | Нет (вызывается только finalizer'ом после verification) | LLM не может вызвать напрямую |

## Реальное действие (спроектировано, не реализован executor)

| action_type | Статус | risk_level | required_approval | Примечание |
|---|---|---|---|---|
| `provision_webitel_user` | В Action Registry, executor не реализован | HIGH | Да | См. ниже |

`provision_webitel_user` спроектирован по итогам read-only разведки реальной production-системы (2026-09-04/07, Astana Motors, Creatio + Webitel Almaty). Реальный сценарий заявки "Доступ к ИС (Информационные системы)" — не смена имени, а провижининг нового Webitel-пользователя по образцу существующего ("соседнего") пользователя:

- **Копируется с пользователя-образца**: `roles` (набор ролей), `license` (набор лицензий), и только поле `group` из вкладки Variables.
- **Не копируется, вводится заново**: General info — имя, логин, extension нового сотрудника.
- **Пароль не является аргументом плана** — он генерируется вживую кнопкой "generate" в Webitel во время исполнения (Stage 7, не реализован), а не решается заранее и не запекается в план/hash. `temporary_password` (принудительный сброс при первом входе) — булев аргумент плана, потому что это решение, которое должен увидеть подтверждающий.

Обязательные аргументы: `template_user_login`, `new_user_login`, `new_user_name`, `new_user_extension`, `roles`, `license`, `group`, `temporary_password`. Разрешён только для `ticket_type = access_to_information_system`.

Не реализовано: Entity Resolver для Webitel (чтение текущих roles/license/group шаблонного пользователя), Plan Compiler для этого действия, executor (реальный клик "Save" в Webitel), verifier. Реальные, подтверждённые Playwright-селекторы — в `src/crm_ai_agent/adapters/webitel_playwright/selectors.py`.

## Устаревшие спекулятивные записи (из исходного ТЗ, до реальной разведки)

Эти действия были частью первоначального гипотетического ТЗ, написанного до того, как стало известно, как выглядит реальная система. Оставлены для истории; реальный сценарий (`provision_webitel_user`) их частично перекрывает, но не идентичен им — не полагаться на эту таблицу при проектировании.

| action_type | Статус | risk_level | Примечание |
|---|---|---|---|
| `link_crm_user_to_webitel` | Не реализовано, вероятно устарело | MEDIUM-HIGH | Реальный сценарий не разделяет "линковку" отдельно от провижининга |
| `assign_license` | Не реализовано, вероятно устарело | HIGH | Реальность: лицензии — часть `provision_webitel_user`, не отдельное действие |
| `change_phone_assignment` | Не реализовано | HIGH | Не подтверждено разведкой |
| `create_user` | Не реализовано, вероятно устарело | HIGH | Реальность: `provision_webitel_user` — это и есть create-по-образцу |

## Инварианты каталога

- Каждое действие имеет: `required_argument_keys`, `risk_level`, `required_approval`, `allowed_ticket_types`. Полный `argument_schema` с preconditions/verifier/idempotency_strategy/retry_policy для `provision_webitel_user` ещё не спроектирован — только форма аргументов.
- Действия, отсутствующие в этой таблице (и в коде registry), не могут быть выполнены ни при каких обстоятельствах — hard-deny в Policy Engine.
- Изменение этой таблицы должно сопровождаться версией `policy_version` в коде.

## Зависимость от открытых вопросов

Точные условия закрытия заявки (вопрос 8 в `open_questions.md`) и права на подтверждение (вопросы 4-5) всё ещё не отвечены и нужны до реализации executor'а для `provision_webitel_user`.
