#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

usage() {
    printf 'Использование: %s [--help]\nОбновить текущую ветку (git pull --ff-only), собрать feedback_templater и запустить сервис через Docker Compose.\n' "$0"
}

if [[ "${1:-}" == '--help' || "${1:-}" == '-h' ]]; then
    usage
    exit 0
fi
if (( $# > 0 )); then
    usage >&2
    exit 2
fi

trap 'build_status=$?; printf "Сборка/запуск прерваны: ошибка в строке %s.\n" "$LINENO" >&2; exit "$build_status"' ERR

main() {
    script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
    project_dir="$(cd -- "$script_dir/.." && pwd)"
    cd -- "$project_dir"

    if ! command -v git >/dev/null 2>&1; then
        printf 'Git не найден. Установите Git для обновления проекта.\n' >&2
        exit 1
    fi
    script_before_pull=$(cat -- "$script_dir/build-and-run.sh")
    printf 'Обновление текущей ветки проекта…\n'
    git pull --ff-only
    if [[ "$script_before_pull" != "$(cat -- "$script_dir/build-and-run.sh")" ]]; then
        printf 'Скрипт обновился; запуск новой версии…\n'
        exec bash "$script_dir/build-and-run.sh"
    fi
    APP_COMMIT=$(git rev-parse --short=7 HEAD)
    export APP_COMMIT

    if ! command -v docker >/dev/null 2>&1; then
        printf 'Docker не найден. Установите Docker и плагин Docker Compose.\n' >&2
        exit 1
    fi
    docker compose version >/dev/null
    docker info >/dev/null
    docker compose config --quiet

    printf 'Сборка образа feedback_templater из коммита %s…\n' "$APP_COMMIT"
    docker build --build-arg "APP_COMMIT=$APP_COMMIT" --tag feedback_templater .

    if ! docker network inspect moc >/dev/null 2>&1; then
        printf 'Создание сети moc…\n'
        docker network create moc
    fi
    mkdir -p -- "$project_dir/instance"

    printf 'Запуск сервиса…\n'
    docker compose up --detach --force-recreate feedback_templater
    docker compose exec -T feedback_templater python -c '
import os, sys
from src.version import commit_hash
expected = sys.argv[1]
actual = os.environ.get("APP_COMMIT", "")
if actual != expected or commit_hash() != expected:
    sys.exit(f"Ошибка версии: ожидался {expected}, в контейнере {actual or chr(8212)}")
print(f"Версия контейнера подтверждена: {actual}")
    ' "$APP_COMMIT"
    docker compose ps feedback_templater
}

main
