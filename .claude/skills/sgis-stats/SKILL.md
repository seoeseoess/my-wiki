---
name: sgis-stats
description: SGIS(통계지리정보서비스, 국가데이터처) 오픈API로 시도·시군구·읍면동 단위 통계를 받을 때 사용한다. 성·연령별 인구(청년 20~34세 포함), 사업체수·종사자수(산업별), 행정구역 경계 GeoJSON, 행정구역코드 찾기를 다룬다. 사용자가 "SGIS", "통계지리정보", "시군구별 인구", "연령별 인구", "청년 인구", "사업체 수", "종사자 수", "행정구역 경계", "행정동 코드", "지도로 그릴 경계" 등을 말하거나, 청년 유출·지방소멸 분석에 지역별 인구·일자리 데이터가 필요하면 스킬 이름을 말하지 않아도 사용한다. 인구 이동(전입·전출)은 SGIS에 없으므로 KOSIS를 쓴다.
---

# SGIS 통계 받기

국가데이터처 SGIS 오픈API를 `scripts/sgis.py` 하나로 부른다. 표준 라이브러리만 쓰므로 설치할 것이 없다.

## 인증키 — 절대 지키는 규칙

- 키는 프로젝트 루트 `.env` 에만 있다. 이름은 `SGIS_CONSUMER_KEY`(서비스 ID), `SGIS_CONSUMER_SECRET`(보안 Key).
- `.env` 를 열어 읽거나, 키 값을 화면·파일·커밋 메시지·코드에 쓰지 않는다. 스크립트가 알아서 읽는다.
- 사용자가 대화창에 키를 붙여 넣으면 쓰지 말고 `.env` 에 직접 넣으라고 안내한다. 이미 노출됐다면 SGIS에서 재발급을 권한다.
- 커밋 전에 `.gitignore` 에 `.env` 가 있는지, 올라갈 목록에 `.env` 가 없는지 확인한다.

## 토큰 — 사람이 만지지 않는다

accessToken은 4시간 유효하다. 스크립트가 처리한다.
- OS 임시 폴더(`sgis_token_cache.json`, 저장소 밖)에 캐시하고, 만료 10분 전이면 새로 발급한다.
- 호출 중 `errCd -401`(인증 실패·만료)이 오면 즉시 재발급해 한 번 더 부른다.
- 사용자에게 토큰을 받아 오라고 하지 않는다. 상태만 볼 때는 `python .claude/skills/sgis-stats/scripts/sgis.py token`.

## 절차

1. **지역코드 찾기** — 코드가 확실하지 않으면 먼저 찾는다. 코드를 추측하지 않는다.
   ```
   python .claude/skills/sgis-stats/scripts/sgis.py codes                  # 시도 목록
   python .claude/skills/sgis-stats/scripts/sgis.py codes --cd 33          # 충북 시군구
   python .claude/skills/sgis-stats/scripts/sgis.py codes --cd 33 --find 청주
   ```
   SGIS 코드는 행정안전부 코드와 다르다(예: 서울 11, 충북 33). 2자리 시도, 5자리 시군구, 8자리 읍면동.

2. **받기** — 프로젝트 루트에서 실행한다. 결과는 `data/sgis/` 에 저장된다.
   ```
   # 성·연령별 인구 (인구주택총조사, 2015~2024)
   python .claude/skills/sgis-stats/scripts/sgis.py population --adm-cd 33 --year 2024 --youth
   python .claude/skills/sgis-stats/scripts/sgis.py population --adm-cd 33040 --age 32,33 --gender 2

   # 사업체수·종사자수 (전국사업체조사, 2000~2024), 산업분류 선택
   python .claude/skills/sgis-stats/scripts/sgis.py company --adm-cd 33 --year 2024
   python .claude/skills/sgis-stats/scripts/sgis.py company --adm-cd 33 --class-code J

   # 행정구역 경계 GeoJSON (2000~2025, UTM-K EPSG:5179)
   python .claude/skills/sgis-stats/scripts/sgis.py boundary --adm-cd 33 --year 2024
   ```
   - `--adm-cd` 를 비우면 전국 시도, `--low-search` 기본 1은 한 단계 아래 지역 목록이다. 지역 자체 값만 원하면 `--low-search 0`.
   - `--youth` 는 15~19, 20~24, 25~29, 30~34세를 따로 받고 `청년_20_34세`(20~34 합계)를 더한다. 청년 기준을 19~34세 등으로 바꿔야 하면 사용자에게 말하고 연령 코드를 조정한다(SGIS는 5세 단위라 19세만 따로 뽑을 수 없다).
   - 연령타입코드: 01=0~4세 … 04=15~19, 05=20~24, 06=25~29, 07=30~34, 08=35~39 … 13=60~64, 24=65세이상, 31=10대, 32=20대, 33=30대.
   - 하루 50,000회 제한이 있다. 전국 읍면동을 연령마다 반복 호출하는 식의 대량 요청은 먼저 호출 수를 사용자에게 알린다.

3. **자릿수 확인** — 스크립트가 출력하는 행 수와 합계를 보고 그럴듯한지 판단해 사용자에게 말한다.
   - 상위 지역 값과 하위 지역 합이 맞는지 본다. 필요하면 `--low-search 0` 으로 상위 값을 받아 대조한다.
   - 합계가 알려진 규모의 두 배 가까우면 상위·하위 행을 함께 더한 것이다(예: 청주시 약 85만 명).
   - 0행이면 코드·연도 조합을 의심한다(행정구역 개편, 연도 범위 밖).

4. **출처 붙이기** — 결과를 보여 줄 때마다 스크립트가 출력한 `출처:` 줄을 그대로 붙인다. 형식: API 이름, 기관(국가데이터처 통계지리정보서비스), 조회일. CSV에는 `출처_API`, `출처_기관`, `조회일` 열이, GeoJSON에는 `source` 항목이 이미 들어 있다. 위키나 보고서로 옮길 때도 이 세 가지를 지운 채 옮기지 않는다.

## 오류가 나면

- `인증키가 없습니다` → 사용자에게 `.env` 에 두 값을 넣으라고 안내한다.
- `인증 실패` → 키 오타나 사용기간 만료. SGIS 개발지원센터 마이페이지에서 확인하게 한다.
- `SGIS 서버에 연결하지 못했습니다` → 네트워크 문제. 같은 명령을 여러 번 반복하지 않는다.
- 기본 주소는 `https://sgisapi.mods.go.kr/OpenAPI3` 다. 기관 도메인이 바뀌면 `.env` 가 아니라 환경변수 `SGIS_BASE_URL` 로 바꾼다.

## 이 스킬로 못 하는 것

- 전입·전출(인구 이동), 이동 사유 → KOSIS 국내인구이동통계.
- 고용률 → KOSIS 지역별고용조사.
- SGIS의 다른 API(가구·주택, 지오코딩, 격자, 자연재해 등)가 필요하면 `scripts/sgis.py` 의 `call()` 을 그대로 쓰고 `API_NAMES` 에 이름을 추가해 출처가 빠지지 않게 한다. 문서: https://sgis.mods.go.kr/developer/html/newOpenApi/api/dataApi/basics.html
