"""
평일(공휴일 제외) 17:30 KST에 실행되어,
'오늘 18:00 ~ 다음 근무일(평일이면서 공휴일이 아닌 날) 09:00' 사이에
원주시 단구동에 비 또는 눈이 예보되어 있으면 텔레그램으로 알림을 보낸다.

예) 1일(평일) 2일(평일) 3일(공휴일) 4일(평일) 5~6일(공휴일) 7일(평일)
  - 1일 17:30 → 1일 18:00 ~ 2일 09:00
  - 2일 17:30 → 2일 18:00 ~ 4일 09:00
  - 4일 17:30 → 4일 18:00 ~ 7일 09:00

날씨 데이터: Open-Meteo (무료, API 키 불필요) https://open-meteo.com
공휴일 데이터: 파이썬 holidays 패키지 (대체공휴일 포함)
"""

import os
import sys
import datetime as dt
import requests
import holidays

# ---------------- 설정 ----------------
# 원주시 단구동 대략 좌표 (날씨 격자가 수 km 단위라 약간의 오차는 영향이 거의 없음)
LATITUDE = 37.32
LONGITUDE = 127.94
PLACE_NAME = "원주시 단구동"

WINDOW_START_HOUR = 18   # 당일 18시부터
WINDOW_END_HOUR = 9      # 다음 근무일 09시까지

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# WMO 날씨 코드 → 종류
# https://open-meteo.com/en/docs (WMO Weather interpretation codes)
SNOW_CODES = {71, 73, 75, 77, 85, 86}
RAIN_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}

KST = dt.timezone(dt.timedelta(hours=9))


# ---------------- 날짜 계산 ----------------
def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)


def is_working_day(day: dt.date, kr_holidays) -> bool:
    """평일(월~금)이면서 공휴일이 아니면 True."""
    return day.weekday() < 5 and day not in kr_holidays


def get_kr_holidays(year: int):
    # 연말/연초에 걸칠 수 있으므로 올해와 다음 해를 함께 불러온다.
    return holidays.country_holidays("KR", years=[year, year + 1])


def next_working_day(day: dt.date, kr_holidays) -> dt.date:
    d = day + dt.timedelta(days=1)
    while not is_working_day(d, kr_holidays):
        d += dt.timedelta(days=1)
    return d


def get_window(today: dt.date, kr_holidays):
    """(시작 datetime, 끝 datetime) 반환. 예: 오늘 18:00 ~ 다음 근무일 09:00"""
    end_day = next_working_day(today, kr_holidays)
    start = dt.datetime.combine(today, dt.time(WINDOW_START_HOUR, 0), tzinfo=KST)
    end = dt.datetime.combine(end_day, dt.time(WINDOW_END_HOUR, 0), tzinfo=KST)
    return start, end


# ---------------- 날씨 조회 ----------------
def fetch_hourly_weather():
    """Open-Meteo에서 시간별 예보(한국시간)를 가져온다."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "hourly": "weather_code,precipitation,snowfall,precipitation_probability",
        "timezone": "Asia/Seoul",
        "forecast_days": 16,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()["hourly"]


def classify(code: int, precipitation: float, snowfall: float):
    """시간별 데이터를 '눈' / '비' / None 으로 분류한다."""
    if code in SNOW_CODES or (snowfall or 0) > 0:
        return "눈"
    if code in RAIN_CODES or (precipitation or 0) > 0:
        return "비"
    return None


def find_precip_hours(hourly, start: dt.datetime, end: dt.datetime):
    """구간(start <= t < end) 안에서 비/눈이 예보된 시간 목록을 반환한다."""
    result = []  # (datetime, '눈' or '비', 강수확률)
    times = hourly["time"]
    for i, t_str in enumerate(times):
        t = dt.datetime.fromisoformat(t_str).replace(tzinfo=KST)
        if not (start <= t < end):
            continue
        kind = classify(
            hourly["weather_code"][i],
            hourly["precipitation"][i],
            hourly["snowfall"][i],
        )
        if kind:
            prob = hourly["precipitation_probability"][i]
            result.append((t, kind, prob))
    return result


# ---------------- 메시지 만들기 ----------------
def fmt(t: dt.datetime) -> str:
    wd = "월화수목금토일"[t.weekday()]
    return f"{t.month}/{t.day}({wd}) {t.hour:02d}시"


def group_ranges(items):
    """같은 종류가 연속된 시간들을 (종류, 시작, 끝, 최대확률) 구간으로 묶는다."""
    ranges = []
    for t, kind, prob in items:
        if ranges and ranges[-1]["kind"] == kind and t - ranges[-1]["end"] == dt.timedelta(hours=1):
            ranges[-1]["end"] = t
            if prob is not None:
                ranges[-1]["prob"] = max(ranges[-1]["prob"] or 0, prob)
        else:
            ranges.append({"kind": kind, "start": t, "end": t, "prob": prob})
    return ranges


def build_message(start, end, items):
    # 예보된 종류에 따라 제목 앞 이모지 결정
    kinds = {k for _, k, _ in items}
    if kinds == {"눈"}:
        icon = "❄️"
    elif kinds == {"비"}:
        icon = "🌧️"
    else:
        icon = "🌨️"  # 비와 눈이 모두 예보된 경우

    lines = [f"{icon} 눈/비 알림", ""]

    # 비/눈이 연속된 구간별로 2줄씩 표시
    for r in group_ranges(items):
        # 끝 시각은 '그 시간대가 끝나는 시각'으로 표시하기 위해 1시간을 더한다.
        end_show = r["end"] + dt.timedelta(hours=1)
        prob = f"(강수확률 최대 {r['prob']}%)" if r["prob"] is not None else ""
        lines.append(f"· {r['kind']}{prob}")
        lines.append(f"· {fmt(r['start'])} ~ {fmt(end_show)}")
        lines.append("")

    lines.append(f"· 확인 구간 : {fmt(start)} ~ {fmt(end)}")
    lines.append(f"· 확인 장소 : {PLACE_NAME}")
    return "\n".join(lines)


# ---------------- 텔레그램 ----------------
def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("텔레그램 TOKEN/CHAT_ID가 설정되지 않았습니다. Secrets를 확인하세요.")
        sys.exit(1)
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=15)
    resp.raise_for_status()


# ---------------- 메인 ----------------
def main():
    manual_mode = os.environ.get("MANUAL_MODE", "")  # '', 'check', 'test', 'auto'
    now = now_kst()
    today = now.date()
    kr_holidays = get_kr_holidays(today.year)

    print(f"실행 시각(KST): {now.strftime('%Y-%m-%d %H:%M')} ({'월화수목금토일'[today.weekday()]})")

    # 수동 실행(check/test)이면 True, 외부 스케줄러(mode=auto)는 '자동 실행'으로 취급
    is_manual = (
        os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        and manual_mode != "auto"
    )

    # 자동 실행은 KST 16:00 ~ 18:59 사이에만 허용 (지연 실행 차단)
    if not is_manual and not (16 <= now.hour < 19):
        print(f"실행 시각({now.strftime('%H:%M')})이 허용 시간대(16~19시)가 아니라 건너뜁니다.")
        return

    # 자동 실행일 때만 '오늘이 근무일인지' 확인 (수동 실행은 건너뜀)
    if not is_manual and not is_working_day(today, kr_holidays):
        reason = kr_holidays.get(today) or "주말"
        print(f"오늘은 쉬는 날({reason})이라 실행하지 않습니다.")
        return

    start, end = get_window(today, kr_holidays)
    # 수동 실행을 17:30 이전/이후 어느 시간에 해도 구간이 일정하도록 start는 '오늘 18:00' 고정
    print(f"확인 구간: {start.strftime('%Y-%m-%d %H:%M')} ~ {end.strftime('%Y-%m-%d %H:%M')}")

    hourly = fetch_hourly_weather()
    items = find_precip_hours(hourly, start, end)

    if items:
        message = build_message(start, end, items)
        print(message)
        send_telegram(message)
        print("텔레그램 알림을 발송했습니다.")
    else:
        print("해당 구간에 비/눈 예보가 없습니다.")
        if manual_mode == "test":
            send_telegram(
                f"☀️ [테스트] {PLACE_NAME}\n"
                f"확인 구간: {fmt(start)} ~ {fmt(end)}\n"
                "이 구간에는 비/눈 예보가 없습니다. (시스템은 정상 작동 중)"
            )
            print("테스트 알림을 발송했습니다.")


if __name__ == "__main__":
    main()
