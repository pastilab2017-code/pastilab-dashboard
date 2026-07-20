# pastilab-dashboard

Ежедневно обновляемая сводная страница по маркетплейсам (Wildberries + Ozon).

Workflow `update-dashboard` раз в сутки собирает данные из API, шифрует страницу
паролем (StatiCrypt, AES-256) и публикует её в ветку `gh-pages`.

- Токены API и пароль страницы — только в Actions Secrets (`WB_CONFIG`,
  `OZON_CONFIG`, `DASHBOARD_PASSWORD`).
- Помесячный кэш данных хранится в GitHub Actions cache и в репозиторий не коммитится.
- Запустить обновление вручную: Actions → update-dashboard → Run workflow.
