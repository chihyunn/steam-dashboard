# Steam Dashboard

**Steam 판매 실시간 모니터링 대시보드. Python 파일 하나, 외부 의존성 제로.**

[![Python 3.8+](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Sponsor](https://img.shields.io/badge/Sponsor-%E2%9D%A4-ea4aaa.svg)](https://github.com/sponsors/chihyunn)

[English](README.md) | **한국어**

![Dashboard](screenshots/dashboard.png)

## 왜 만들었나

> 게임 출시하고 Steamworks 판매 페이지를 하루에 열두 번씩 새로고침하는 제 자신을 발견하고... 그냥 만들어버렸습니다.

판매, 위시리스트, 리뷰, 동접을 한 화면에서 실시간으로 보고, 텔레그램 알림까지 받을 수 있는 셀프 호스팅 대시보드입니다.

## 기능

- **실시간 판매/수익 추적** — 판매, 환불, 순수익 한눈에
- **텔레그램 즉시 알림** — 새 판매, 새 리뷰, 동접 급증
- **국가별 현황** — 판매/위시리스트 국가별 분포
- **동접자 모니터링** — 추이 그래프 + 히스토리
- **위시리스트 추적** — 추가/삭제/구매전환율
- **웹 설정 마법사** — config 파일 편집 필요 없음
- **멀티게임 지원** — 여러 게임 동시 모니터링
- **한국어/영어 UI** — 브라우저 언어 자동 감지
- **모바일 대응** — 핸드폰에서도 판매 현황 확인
- **Python 단일 파일** — pip install도, Docker도 필요 없음
- **100% 셀프 호스팅** — 데이터가 외부로 나가지 않음

## 빠른 시작

```bash
# 1. 클론
git clone https://github.com/chihyunn/steam-dashboard.git
cd steam-dashboard

# 2. 실행
python3 dashboard.py

# 3. 브라우저 열기
# → http://localhost:8081
# → 설정 마법사가 안내해줍니다
```

끝입니다. `requirements.txt`도, `.env` 파일도, DB 설정도 없습니다.

## 스크린샷

| 설정 마법사 | 대시보드 |
|:---:|:---:|
| ![Setup](screenshots/setup-wizard.png) | ![Dashboard](screenshots/dashboard.png) |

| 모바일 | 텔레그램 알림 |
|:---:|:---:|
| ![Mobile](screenshots/mobile.png) | ![Telegram](screenshots/telegram.png) |

## API 키 발급 방법

설정 마법사에서 두 개의 키를 입력해야 합니다.

### Steam Web API Key (공개 데이터용)

동접, 리뷰, 앱 정보 등 공개 데이터에 접근합니다.

1. [steamcommunity.com/dev/apikey](https://steamcommunity.com/dev/apikey) 접속
2. 도메인 이름 아무거나 입력 (예: `localhost`)
3. 키 복사

### Steam Financial API Key (파트너 데이터용)

판매, 수익, 위시리스트 데이터에 접근합니다. 찾기가 좀 어렵습니다.

1. [partner.steamgames.com](https://partner.steamgames.com) 로그인
2. 상단 메뉴에서 **사용자 및 권한** 클릭
3. **그룹 관리** 클릭
4. 퍼블리셔/개발자 그룹 이름 클릭
5. 사이드바에서 **Web API Key** 클릭
6. 키가 없으면 **생성** 클릭
7. 키 복사

> **참고:** 이 키에 접근하려면 Steamworks 그룹에서 "재무 정보 보기" 권한이 필요합니다. Web API Key 옵션이 안 보이면 그룹 관리자에게 요청하세요.

## 텔레그램 설정 (선택)

누가 게임을 사면 핸드폰으로 알림 받으세요.

### 1단계: 봇 만들기

1. 텔레그램에서 [@BotFather](https://t.me/BotFather)에게 메시지
2. `/newbot` 전송
3. 봇 이름 입력 (예: "내 Steam 대시보드")
4. 봇 유저네임 입력 (예: `my_steam_dash_bot`)
5. BotFather가 **봇 토큰**을 줍니다:
   ```
   123456789:AAExampleTokenReplaceWithYourOwn_xxx
   ```
6. 복사해서 저장

### 2단계: Chat ID 확인

1. 텔레그램에서 [@userinfobot](https://t.me/userinfobot)에게 메시지
2. **Chat ID**를 알려줍니다 — `7271353545` 같은 숫자
3. 여러 명이 알림 받으려면 각자의 Chat ID를 받으세요

### 3단계: 봇 시작

1. 텔레그램에서 만든 봇을 찾아서 열기
2. **시작** 버튼 누르기 — 이걸 해야 봇이 메시지를 보낼 수 있음
3. 설정 마법사에서 봇 토큰과 Chat ID 입력

### 알림 종류

- 새 판매 알림 (국가별 현황 포함)
- 새 리뷰 알림
- 동접 급증 알림 (50% 이상 증가)
- 위시리스트 변동 알림 (5개 이상 변동)

## 서버 배포

월 $5 VPS면 충분합니다.

```bash
# 서버에 복사
scp dashboard.py user@server:~/
ssh user@server

# 테스트 실행
python3 dashboard.py

# 백그라운드 실행
nohup python3 dashboard.py &
```

### systemd (권장)

`/etc/systemd/system/steam-dashboard.service` 생성:

```ini
[Unit]
Description=Steam Dashboard
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/ubuntu/dashboard.py
WorkingDirectory=/home/ubuntu
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable steam-dashboard
sudo systemctl start steam-dashboard
```

`http://서버IP:8081`로 접속하면 됩니다.

### 보안 참고

이 대시보드에는 인증 기능이 없습니다. 공개 서버에 배포할 경우 리버스 프록시에 기본 인증을 걸어주세요:

```nginx
location / {
    auth_basic "Steam Dashboard";
    auth_basic_user_file /etc/nginx/.htpasswd;
    proxy_pass http://127.0.0.1:8081;
}
```

또는 방화벽으로 IP를 제한하세요.

## 비교

| 기능 | Steam Dashboard | Steamboard | Steamworks Extras |
|---|:---:|:---:|:---:|
| 셀프 호스팅 | O | O | N/A |
| 외부 의존성 없음 | O | X (Electron) | X (Chrome) |
| 텔레그램 알림 | O | X | X |
| 멀티게임 | O | X | X |
| 모바일 대응 | O | X | X |
| 웹 설정 마법사 | O | X | N/A |

## 설정

모든 설정은 웹 UI에서 합니다. config 파일을 편집할 필요 없습니다.

고급 사용자: 설정은 로컬 SQLite DB(`steam_dashboard.db`)에 저장됩니다. 백업하거나 다른 서버로 옮길 수 있습니다.

## 기술 스택

- **Python 3.8+** — 표준 라이브러리만 사용, 외부 패키지 없음
- **SQLite** — 내장 DB, 설정 불필요
- **Chart.js** — 차트 (CDN)
- **Google Fonts** — 폰트 (CDN)

## 기여

이슈와 PR 환영합니다. 원칙: 파일 하나, 의존성 제로.

PR 제출 전:
- Python 3.8+에서 동작하는지 확인
- 외부 의존성 추가하지 않기
- 설정 마법사 플로우 전체 테스트

## 라이선스

MIT

---

인디 게임 개발자가 커피 마시면서 만들었습니다.
Steamworks 새로고침에서 해방시켜줬다면, [후원을 고려해주세요](https://github.com/sponsors/chihyunn).
