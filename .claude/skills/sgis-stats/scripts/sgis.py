"""SGIS(통계지리정보서비스, 국가데이터처) 오픈API 호출 도구.

감싸는 API
  - 인증          /OpenAPI3/auth/authentication.json   (accessToken, 4시간 유효)
  - 단계별 주소    /OpenAPI3/addr/stage.json             (행정구역코드 찾기)
  - 인구통계       /OpenAPI3/stats/searchpopulation.json (성·연령별 인구, 인구주택총조사)
  - 사업체통계     /OpenAPI3/stats/company.json          (사업체수·종사자수, 전국사업체조사)
  - 행정구역경계   /OpenAPI3/boundary/hadmarea.geojson   (UTM-K, EPSG:5179)

인증키는 프로젝트 루트의 .env 에서만 읽는다 (SGIS_CONSUMER_KEY, SGIS_CONSUMER_SECRET).
토큰은 OS 임시 폴더에 캐시하고, 만료 10분 전이거나 API가 -401을 돌려주면 자동으로 다시 발급한다.
표준 라이브러리만 사용한다 (설치할 패키지 없음).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = os.environ.get("SGIS_BASE_URL", "https://sgisapi.mods.go.kr/OpenAPI3")
AGENCY = "국가데이터처 통계지리정보서비스(SGIS)"
TOKEN_CACHE = Path(tempfile.gettempdir()) / "sgis_token_cache.json"
REFRESH_MARGIN_SEC = 600  # 만료 10분 전에 미리 재발급

API_NAMES = {
    "addr/stage.json": "SGIS 단계별 주소 조회 API (addr/stage)",
    "stats/searchpopulation.json": "SGIS 인구통계 API (stats/searchpopulation, 인구주택총조사)",
    "stats/company.json": "SGIS 사업체통계 API (stats/company, 전국사업체조사)",
    "boundary/hadmarea.geojson": "SGIS 행정구역경계 API (boundary/hadmarea)",
}

# 연령타입코드표 (SGIS 개발지원센터 PplAgeCode)
AGE_CODES = {
    "01": "0~4세", "02": "5~9세", "03": "10~14세", "04": "15~19세", "05": "20~24세",
    "06": "25~29세", "07": "30~34세", "08": "35~39세", "09": "40~44세", "10": "45~49세",
    "11": "50~54세", "12": "55~59세", "13": "60~64세", "14": "65~69세", "15": "70~74세",
    "16": "75~79세", "17": "80~84세", "18": "85~89세", "19": "90~94세", "20": "95~99세",
    "21": "100세 이상", "22": "15세미만", "23": "15~64세", "24": "65세이상", "25": "85세이상",
    "31": "10대", "32": "20대", "33": "30대", "34": "40대", "35": "50대", "36": "60대",
}
YOUTH = ["04", "05", "06", "07"]  # 15~34세 원자료. 청년 20~34세는 05~07 합계로 별도 계산
GENDER = {"0": "전체", "1": "남자", "2": "여자"}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


class SgisError(RuntimeError):
    pass


# ---------------------------------------------------------------- .env
def find_env() -> Path | None:
    starts = [Path.cwd(), Path(__file__).resolve().parent]
    for start in starts:
        for d in [start, *start.parents]:
            p = d / ".env"
            if p.is_file():
                return p
    return None


def load_keys() -> tuple[str, str]:
    key = os.environ.get("SGIS_CONSUMER_KEY", "")
    secret = os.environ.get("SGIS_CONSUMER_SECRET", "")
    env = find_env()
    if env and not (key and secret):
        for line in env.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if k.strip() == "SGIS_CONSUMER_KEY" and not key:
                key = v
            elif k.strip() == "SGIS_CONSUMER_SECRET" and not secret:
                secret = v
    if not key or not secret:
        raise SgisError(
            "인증키가 없습니다. 프로젝트 루트의 .env 에 SGIS_CONSUMER_KEY(서비스 ID)와 "
            "SGIS_CONSUMER_SECRET(보안 Key)을 넣어 주세요. 키를 대화창에 붙여 넣지 마세요."
        )
    return key, secret


# ---------------------------------------------------------------- HTTP
def _get(path: str, params: dict) -> dict:
    url = f"{BASE}/{path}?{urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, '')})}"
    req = urllib.request.Request(url, headers={"User-Agent": "sgis-stats-skill/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise SgisError(f"HTTP {e.code} ({path})") from None
    except urllib.error.URLError as e:
        raise SgisError(f"SGIS 서버에 연결하지 못했습니다: {e.reason}") from None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise SgisError(f"JSON이 아닌 응답 ({path}): {body[:200]}") from None


# ---------------------------------------------------------------- token
def _timeout_to_epoch_sec(v) -> float:
    t = float(v)
    return t / 1000 if t > 1e11 else t  # 문서는 '초', 실제 응답은 밀리초인 경우가 있어 둘 다 처리


def _read_cache():
    try:
        c = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
        if c.get("base") == BASE and time.time() < c["expires"] - REFRESH_MARGIN_SEC:
            return c
    except Exception:
        pass
    return None


def get_token(force: bool = False) -> str:
    if not force:
        c = _read_cache()
        if c:
            return c["token"]
    key, secret = load_keys()
    data = _get("auth/authentication.json", {"consumer_key": key, "consumer_secret": secret})
    if str(data.get("errCd")) != "0":
        raise SgisError(f"인증 실패 (errCd {data.get('errCd')}): {data.get('errMsg')}")
    res = data["result"]
    expires = _timeout_to_epoch_sec(res.get("accessTimeout", time.time() + 4 * 3600))
    TOKEN_CACHE.write_text(
        json.dumps({"token": res["accessToken"], "expires": expires, "base": BASE}), encoding="utf-8"
    )
    return res["accessToken"]


def call(path: str, params: dict) -> dict:
    """토큰을 붙여 호출. 토큰 만료(-401)면 한 번 재발급해서 다시 부른다."""
    for attempt in range(2):
        data = _get(path, {"accessToken": get_token(force=attempt > 0), **params})
        code = str(data.get("errCd"))
        if code == "0":
            return data
        if code == "-401" and attempt == 0:
            continue
        if code == "-100":  # 검색결과 없음
            return {**data, "result": []}
        raise SgisError(f"{path} 오류 (errCd {code}): {data.get('errMsg')}")
    raise SgisError("토큰을 재발급했지만 인증에 계속 실패합니다.")


# ---------------------------------------------------------------- output
def today() -> str:
    return dt.date.today().isoformat()


def source_cols(path: str) -> dict:
    return {"출처_API": API_NAMES[path], "출처_기관": AGENCY, "조회일": today()}


def source_line(path: str) -> str:
    return f"출처: {API_NAMES[path]}, {AGENCY}, 조회일 {today()}"


def write_csv(rows: list[dict], out: Path):
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = list(dict.fromkeys(k for r in rows for k in r))
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def to_int(v):
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return None


def default_out(name: str) -> Path:
    return Path.cwd() / "data" / "sgis" / name


# ---------------------------------------------------------------- commands
def cmd_codes(a):
    path = "addr/stage.json"
    data = call(path, {"cd": a.cd})
    rows = [{"adm_cd": r.get("cd"), "이름": r.get("addr_name"), "전체주소": r.get("full_addr")}
            for r in data.get("result", [])]
    if a.find:
        rows = [r for r in rows if a.find in (r["전체주소"] or "")]
    for r in rows:
        print(f"{r['adm_cd']}\t{r['전체주소']}")
    print(source_line(path))


def cmd_population(a):
    path = "stats/searchpopulation.json"
    ages = YOUTH if a.youth else [x.strip().zfill(2) for x in a.age.split(",")] if a.age else [None]
    table: dict[str, dict] = {}
    for age in ages:
        data = call(path, {"year": a.year, "adm_cd": a.adm_cd, "low_search": a.low_search,
                           "gender": a.gender, "age_type": age})
        for r in data.get("result", []):
            row = table.setdefault(r["adm_cd"], {"year": a.year, "adm_cd": r["adm_cd"],
                                                  "adm_nm": r.get("adm_nm"),
                                                  "성별": GENDER.get(str(a.gender), a.gender)})
            col = f"인구_{AGE_CODES.get(age, age)}" if age else "인구_전체"
            row[col] = to_int(r.get("population"))
    rows = list(table.values())
    if a.youth:
        for r in rows:
            parts = [r.get(f"인구_{AGE_CODES[c]}") for c in ("05", "06", "07")]
            r["청년_20_34세"] = sum(p for p in parts if p is not None) if any(p is not None for p in parts) else None
    for r in rows:
        r.update(source_cols(path))
    out = Path(a.out) if a.out else default_out(
        f"population_{a.adm_cd or 'sido'}_{a.year}{'_youth' if a.youth else ''}.csv")
    write_csv(rows, out)
    summarize(rows, out, path)


def cmd_company(a):
    path = "stats/company.json"
    data = call(path, {"year": a.year, "adm_cd": a.adm_cd, "low_search": a.low_search,
                       "class_code": a.class_code})
    rows = [{"year": a.year, "adm_cd": r.get("adm_cd"), "adm_nm": r.get("adm_nm"),
             "산업분류": a.class_code or "전체", "사업체수": to_int(r.get("corp_cnt")),
             "종사자수": to_int(r.get("tot_worker")), **source_cols(path)}
            for r in data.get("result", [])]
    out = Path(a.out) if a.out else default_out(
        f"company_{a.adm_cd or 'sido'}_{a.year}{'_' + a.class_code if a.class_code else ''}.csv")
    write_csv(rows, out)
    summarize(rows, out, path)


def cmd_boundary(a):
    path = "boundary/hadmarea.geojson"
    data = call(path, {"year": a.year, "adm_cd": a.adm_cd, "low_search": a.low_search})
    gj = {"type": "FeatureCollection",
          "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::5179"}},
          "source": {"api": API_NAMES[path], "agency": AGENCY, "retrieved": today(),
                     "year": a.year, "adm_cd": a.adm_cd},
          "features": data.get("features", [])}
    out = Path(a.out) if a.out else default_out(f"boundary_{a.adm_cd or 'sido'}_{a.year}.geojson")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(gj, ensure_ascii=False), encoding="utf-8")
    names = [f["properties"].get("adm_nm") for f in gj["features"]]
    print(f"경계 {len(names)}개 저장: {out}")
    print("  " + ", ".join(n for n in names[:10] if n) + (" ..." if len(names) > 10 else ""))
    print("좌표계: UTM-K (EPSG:5179)")
    print(source_line(path))


def cmd_token(a):
    get_token(force=a.refresh)
    c = _read_cache()
    left = (c["expires"] - time.time()) / 60 if c else 0
    print(f"토큰 정상. 남은 시간 약 {left:.0f}분 (토큰 값은 출력하지 않음)")


def summarize(rows, out, path):
    print(f"{len(rows)}행 저장: {out}")
    num_cols = [k for k in (rows[0] if rows else {}) if isinstance(rows[0].get(k), int) and k != "year"]
    for r in rows[:15]:
        vals = ", ".join(f"{k}={r[k]:,}" for k in num_cols if r.get(k) is not None)
        print(f"  {r['adm_cd']} {r.get('adm_nm')}: {vals}")
    if len(rows) > 15:
        print(f"  ... 외 {len(rows) - 15}행")
    for k in num_cols:
        tot = sum(r[k] for r in rows if r.get(k) is not None)
        print(f"합계 {k}: {tot:,}")
    print(source_line(path))


def main():
    p = argparse.ArgumentParser(description="SGIS 오픈API 도구")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("codes", help="행정구역코드 찾기 (단계별 주소)")
    s.add_argument("--cd", default="", help="비우면 시도, 2자리면 그 시도의 시군구, 5자리면 읍면동")
    s.add_argument("--find", help="전체주소에 이 글자가 들어간 것만")
    s.set_defaults(fn=cmd_codes)

    s = sub.add_parser("population", help="성·연령별 인구 (인구주택총조사 2015~2024)")
    s.add_argument("--adm-cd", default="", help="비우면 전국 시도, 2자리 시도, 5자리 시군구, 8자리 읍면동")
    s.add_argument("--year", default="2024")
    s.add_argument("--low-search", default="1", choices=["0", "1", "2"])
    s.add_argument("--gender", default="0", choices=["0", "1", "2"])
    g = s.add_mutually_exclusive_group()
    g.add_argument("--age", help="연령타입코드, 쉼표로 여러 개 (예: 05,06,07)")
    g.add_argument("--youth", action="store_true", help="15~19·20~24·25~29·30~34세 + 청년(20~34) 합계")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_population)

    s = sub.add_parser("company", help="사업체수·종사자수 (전국사업체조사 2000~2024)")
    s.add_argument("--adm-cd", default="")
    s.add_argument("--year", default="2024")
    s.add_argument("--low-search", default="1", choices=["0", "1", "2"])
    s.add_argument("--class-code", help="산업분류코드 (예: C 제조업, J 정보통신업)")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_company)

    s = sub.add_parser("boundary", help="행정구역 경계 GeoJSON (2000~2025)")
    s.add_argument("--adm-cd", default="")
    s.add_argument("--year", default="2024")
    s.add_argument("--low-search", default="1", choices=["0", "1", "2"])
    s.add_argument("--out")
    s.set_defaults(fn=cmd_boundary)

    s = sub.add_parser("token", help="토큰 상태 확인 (값은 출력하지 않음)")
    s.add_argument("--refresh", action="store_true")
    s.set_defaults(fn=cmd_token)

    a = p.parse_args()
    try:
        a.fn(a)
    except SgisError as e:
        print(f"오류: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
