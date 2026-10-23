#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

usage() {
    cat <<HELP
Использование: $0 [--force | --install-systemd | --help]
Без флагов: git pull --ff-only; сборка и запуск только при новых коммитах
либо после неудачного обновления. Одновременные запуски пропускаются.
--force           Собрать и запустить даже без новых коммитов.
--install-systemd Установить и включить пользовательский таймер (30 секунд).
HELP
}

force=false
install_systemd=false
case "${1:-}" in
    --help|-h) usage; exit 0 ;;
    --force) force=true ;;
    --install-systemd) install_systemd=true ;;
    '') ;;
    *) usage >&2; exit 2 ;;
esac
if (( $# > 1 )); then usage >&2; exit 2; fi

trap 'build_status=$?; printf "Обновление/запуск прерваны: ошибка в строке %s.\n" "$LINENO" >&2; exit "$build_status"' ERR

install_units() {
    if ! command -v systemctl >/dev/null 2>&1; then
        printf 'systemctl не найден. Требуется systemd с пользовательским менеджером.\n' >&2
        return 1
    fi
    systemctl --user show-environment >/dev/null
    local unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    local service=feedback-templater-update.service
    local timer=feedback-templater-update.timer
    local escaped_project="$project_dir"
    if [[ "$escaped_project" == *$'\n'* || "$escaped_project" == *$'\r'* ]]; then
        printf 'Путь проекта не должен содержать перевод строки.\n' >&2
        return 1
    fi
    escaped_project=${escaped_project//\\/\\\\}
    escaped_project=${escaped_project//\"/\\\"}
    escaped_project=${escaped_project//%/%%}
    mkdir -p -- "$unit_dir/$service.d"
    install -m 644 "$script_dir/systemd/$service" "$unit_dir/$service"
    install -m 644 "$script_dir/systemd/$timer" "$unit_dir/$timer"
    cat > "$unit_dir/$service.d/10-project.conf" <<UNIT
[Service]
WorkingDirectory="$escaped_project"
ExecStart=
ExecStart=/bin/bash "$escaped_project/scripts/build-and-run.sh"
UNIT
    chmod 644 "$unit_dir/$service.d/10-project.conf"
    systemctl --user daemon-reload
    systemctl --user enable --now "$timer"
    printf 'Таймер установлен для %s: проверка через 30 секунд после завершения запуска.\n' "$project_dir"
    printf 'Статус: systemctl --user status %s\n' "$timer"
    printf 'Для работы после выхода из сессии: sudo loginctl enable-linger %s\n' "$(id -un)"
}

main() {
    script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
    project_dir="$(cd -- "$script_dir/.." && pwd)"
    cd -- "$project_dir"
    if "$install_systemd"; then install_units; return; fi

    for required_command in git flock; do
        if ! command -v "$required_command" >/dev/null 2>&1; then
            printf 'Не найдена команда: %s.\n' "$required_command" >&2
            return 1
        fi
    done
    state_dir=$(git rev-parse --git-path feedback-templater-update)
    mkdir -p -- "$state_dir"
    if [[ "${FEEDBACK_TEMPLATER_LOCK_HELD:-}" != "$project_dir" || ! -e /proc/$$/fd/9 ]]; then
        exec 9>"$state_dir/update.lock"
        if flock -n -E 75 9; then
            export FEEDBACK_TEMPLATER_LOCK_HELD="$project_dir"
        else
            lock_status=$?
            if (( lock_status == 75 )); then
                printf 'Обновление уже выполняется; пропускаем запуск.\n'
                return 0
            fi
            return "$lock_status"
        fi
    fi

    pending_file="$state_dir/pending"
    before_commit=$(git rev-parse HEAD)
    script_before_pull=$(cat -- "$script_dir/build-and-run.sh")
    printf 'Обновление текущей ветки проекта…\n'
    git pull --ff-only
    after_commit=$(git rev-parse HEAD)
    if [[ "$before_commit" != "$after_commit" ]]; then
        printf '%s\n' "$after_commit" > "$pending_file"
    fi
    if [[ "$script_before_pull" != "$(cat -- "$script_dir/build-and-run.sh")" ]]; then
        printf 'Скрипт обновился; запуск новой версии…\n'
        exec bash "$script_dir/build-and-run.sh" "$@"
    fi
    if ! "$force" && [[ ! -e "$pending_file" ]]; then
        printf 'Новых коммитов нет; сборка и перезапуск не требуются.\n'
        return 0
    fi
    # Оставляем маркер до подтверждения версии: следующая проверка повторит сбой.
    printf '%s\n' "$after_commit" > "$pending_file"
    APP_COMMIT=$(git rev-parse --short=7 HEAD)
    export APP_COMMIT

    if ! command -v docker >/dev/null 2>&1; then
        printf 'Docker не найден. Установите Docker и плагин Docker Compose.\n' >&2
        return 1
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
    rm -f -- "$pending_file"
    docker compose ps feedback_templater
}

main "$@"
