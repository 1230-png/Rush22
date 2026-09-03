# Rush22

한국 개발자 대상 기술 쇼츠를 하루 두 번(07:00 / 18:00 KST) 자동 발행합니다.
채널: [Rush22](https://www.youtube.com/@Rush22-i8p)

무료 스택만 씁니다 — Gemini, edge-tts, Pillow, MoviePy, GitHub Actions.
결제 수단을 등록해야 하는 구간이 없습니다.

## 구조

```
playbook.py        콘텐츠 설계 6축 — 구조·포맷·가치·훅 규칙이 전부 여기
auto_pipeline.py   진입점: 주제 → 대본 → 음성 → 영상 → 업로드 → 기록
render.py          배경 플레이트, 단어 싱크 자막, 진행 바
topics.py          주제 뱅크 관리 (소진 시 자동 보충)
health_check.py    발행이 멈췄는지 판정 (멈추면 이슈 자동 생성)
selftest.py        Gemini·업로드 없이 렌더러만 검증
check_refill.py    보충 경로만 시험 (파일 미변경)
topic_bank.json    주제 목록 — 자동으로 늘어남
used_log.csv       발행 기록 (Actions가 자동 커밋)
```

채널 방향을 바꾸려면 `playbook.py`만 고치면 됩니다. 나머지 코드는 주제도
포맷도 모릅니다.

발행 주기는 [`.github/workflows/daily_build.yml`](.github/workflows/daily_build.yml)에 있습니다.

## 한 편이 만들어지는 과정

1. `used_log.csv`와 대조해 아직 안 쓴 주제를 고릅니다. 잔량이 12개 밑이면
   Gemini에게 40개를 더 받아 채웁니다 — 전체 발행 이력을 프롬프트에 넣어
   중복을 막습니다.
2. Gemini가 후킹 → 원리 → 실무 변화 3단 구조로 대본을 씁니다.
3. edge-tts가 음성과 함께 **단어별 타임스탬프**를 내보냅니다.
4. 그 타임스탬프로 자막을 단어 단위로 동기화하고, 배경을 천천히 밀면서
   합성합니다.
5. YouTube에 공개 업로드하고 `used_log.csv`에 기록합니다.

업로드가 영상 ID를 돌려준 행만 "사용 완료"로 셉니다. 중간에 실패한 실행은
주제를 소진하지 않고, 다음 실행이 같은 주제를 재시도합니다.

## 필요한 GitHub Secrets

저장소 → Settings → Secrets and variables → Actions

| 이름 | 용도 |
|---|---|
| `GEMINI_API_KEY` | 대본·주제 생성 |
| `YOUTUBE_CLIENT_ID` | OAuth 클라이언트 |
| `YOUTUBE_CLIENT_SECRET` | 〃 |
| `YOUTUBE_REFRESH_TOKEN` | 업로드 인증 |

> OAuth 동의 화면이 **"테스트"** 상태로 남아 있으면 Google이 refresh token을
> 7일마다 폐기합니다. 첫 주만 정상 동작하다가 8일째부터 전부 `invalid_grant`으로
> 실패하는, 코드에는 아무 문제가 없어서 원인을 찾기 어려운 종류의 고장입니다.
> **프로덕션으로 게시된 상태를 유지하세요.**

## 로컬에서 확인하기

```bash
pip install -r requirements.txt
python selftest.py     # 렌더링만 검증 (자격증명 불필요)
```

`selftest_out/frame_mid.png`에 중간 프레임이 떨어집니다. 자막 위치나 배경을
건드린 뒤 눈으로 확인할 때 씁니다.

> edge-tts는 일부 클라우드 IP에서 음성 서버가 차단됩니다. 로컬에서 막히면
> 합성 타이밍으로 자동 대체되며, GitHub Actions 러너에서는 정상 동작합니다.

## 주제 바꾸기

채널 주제를 통째로 바꾸려면 `topic_bank.json`을 교체하고 `topics.py`의
`generate_topics` 프롬프트만 수정하면 됩니다. 나머지 코드는 건드릴 필요가
없습니다.

## 수익화

목표와 실제 도달 가능성은 [`ROADMAP.md`](ROADMAP.md)에 정리해 두었습니다.
요약하면 **한 달 안에는 규정상 불가능**합니다 — 쇼츠 경로의 측정 창이 90일이라
30일차에는 창이 닫히지도 않습니다.
