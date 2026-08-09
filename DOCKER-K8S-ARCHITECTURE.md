# Docker 실행 및 배포 가이드

## 목적과 진행 순서

이 문서는 Windows 개발 PC에서 검증한 Docker 작업과 이후 배포 순서를 기록한다.

1. 로컬 Docker 실행 기반 확정
2. 면접 시연 때만 named Cloudflare Tunnel 연결
3. EC2, GitHub Actions, Kubernetes는 운영 학습 단계로 보류

Kubernetes부터 시작하지 않는다. 단일 서버에서도 실행되지 않는 이미지를 Kubernetes에 올리면 장애 원인만 `Docker`, 네트워크, Pod, Service로 분산되기 때문이다.

요청에 따라 이미지 저장소와 컨테이너 이름은 `eu-duty-automatation`을 사용한다. `automatation`은 일반적인 영어 `automation`과 철자가 다르다. ECR 저장소를 만든 뒤 이름을 바꾸면 새 저장소와 배포 설정이 필요하므로 AWS 작업 전에 최종 철자를 확정한다.

## Image와 Container의 차이

Docker image는 C++ 기준으로 실행 파일, DLL, 런타임, 정적 리소스를 묶은 읽기 전용 배포 패키지다. Container는 그 image에서 생성된 실제 실행 프로세스와 쓰기 가능한 임시 파일시스템이다.

- `eu-duty-automatation:local`: 보존할 최종 로컬 image
- `eu-duty-automatation`: 앞으로 `docker run --name`에 사용할 container 이름
- image를 삭제하면 다시 빌드하거나 registry에서 받아야 한다.
- container를 삭제해도 image는 남는다.
- `--rm`으로 시작한 container는 중지될 때 자동 삭제된다.

## Step 1: 설정 경로 교정

### 의미

Windows 호스트의 프로젝트 경로와 Linux container 경로는 서로 다르다. Dockerfile의 `WORKDIR /app`과 `COPY . /app` 때문에 container 안의 프로젝트 루트는 `/app`이다. 따라서 비밀이 아닌 앱 설정은 다음 경로에서 읽어야 한다.

```text
/app/config/.appconfig.asap_app.toml
```

`asap_app.py`는 다음 규칙을 사용하도록 수정했다.

- 기본 설정: `<project-root>/config/.appconfig.asap_app.toml`
- 설정 재정의: `ASAP_APP_CONFIG_PATH`
- dotenv 재정의: `ASAP_ENV_FILE`
- 상대경로 재정의는 현재 작업 디렉터리가 아니라 프로젝트 루트 기준으로 해석

이는 DirectX 프로그램이 실행 시 현재 디렉터리에 의존하지 않고 확정된 asset root를 사용하는 것과 같은 목적이다.

### 검증 명령

```powershell
conda run -n asap_pw python -m pytest tests/test_asap_app_config_paths.py -q
docker run --rm --entrypoint sh eu-duty-automatation:local -c 'test -f /app/config/.appconfig.asap_app.toml && echo CONFIG_FILE_OK'
docker run --rm --entrypoint /opt/conda/envs/asap_pw/bin/python eu-duty-automatation:local -c 'import asap_app; print(asap_app.ASAP_APP_CONFIG_PATH)'
```

성공 기준은 테스트 통과, `CONFIG_FILE_OK`, `/app/config/.appconfig.asap_app.toml` 출력이다.

## Step 2: 운영 WSGI 서버 적용

### 의미

Flask는 HTTP API framework이고 `flask run`은 개발용 실행기다. 운영 container에서는 listening socket, 종료 signal, worker 생명주기를 관리하는 WSGI server가 필요하다. `environments.yml`에 `gunicorn`을 추가하고 Dockerfile의 실행 명령을 Gunicorn으로 변경했다.

현재 실행 정책은 다음과 같다.

```text
Gunicorn master
└─ worker process 1개
   └─ gthread 4개
```

worker를 1개로 제한하는 이유는 `RunRegistry`, job thread, SSE event buffer가 process memory에 있기 때문이다. worker가 둘이면 같은 `job_id`를 서로 다른 메모리에서 조회할 수 있다. thread 4개는 SSE 연결이 유지되는 동안 health 및 API 요청을 받을 최소 동시성이다. 이 값은 성능 최적화 결과가 아니라 현재 상태 모델을 지키는 안전한 시작값이다.

### Dockerfile 실행 계약

```dockerfile
CMD ["/opt/conda/envs/asap_pw/bin/gunicorn", \
    "--bind=0.0.0.0:8060", \
    "--workers=1", \
    "--worker-class=gthread", \
    "--threads=4", \
    "--timeout=120", \
    "--access-logfile=-", \
    "asap_app:app"]
```

성공 기준은 로그의 `Using worker: gthread`, `Booting worker`와 `/api/health`의 `{"status":"ok"}`다. Flask 개발 서버 경고가 나오면 이전 image를 실행한 것이다.

## Step 3: `.env` 없는 런타임 설정 주입

### 의미

Docker의 `--env NAME`은 호스트 process environment 값을 container의 PID 1에 전달한다. Windows의 `CreateProcess`에 environment block을 넘기는 것과 같다. Python은 `os.environ`에서 값을 읽고, `LoadEnvironmentFile()`은 이미 존재하는 process 값을 덮어쓰지 않는다.

`.dockerignore`는 `.env*`와 `**/.env*`를 build context에서 제외한다. `ASAP_ENV_FILE=/dev/null`은 container가 dotenv 파일 없이 process environment만 사용한다는 점을 명시한다.

### 안전한 PowerShell 입력

값을 명령행에 직접 쓰지 않고 보안 입력으로 받는다.

```powershell
$secureDbUrl = Read-Host "ASAP_DATABASE_URL" -AsSecureString
$env:ASAP_DATABASE_URL = [System.Net.NetworkCredential]::new("", $secureDbUrl).Password

docker run --rm -d --name eu-duty-automatation -p 8060:8060 `
  --env ASAP_DATABASE_URL `
  --env ASAP_APP_CONFIG_PATH=/app/config/.appconfig.asap_app.toml `
  --env ASAP_ENV_FILE=/dev/null `
  eu-duty-automatation:local
```

PowerShell의 backtick 뒤에는 공백을 넣지 않는다. 한 줄로 실행해도 동일하다. 분류 DB가 별도라면 `ASAP_CLASSIFICATION_DATABASE_URL`을 사용한다. 현재 TOML의 전체 pipeline은 선택된 profile에 따라 `EU_EXPORT_GOOGLE_AI_STUDIO_KEY`, `EU_EXPORT_OPENAI_KEY`, `EU_EXPORT_ANTHROPIC_KEY`도 요구할 수 있으며 같은 방식으로 주입한다.

### 값 노출 없는 검증

```powershell
docker exec eu-duty-automatation sh -c 'test -n "$ASAP_DATABASE_URL" && echo DATABASE_CONFIG_OK'
docker exec eu-duty-automatation sh -c 'find /app -type f -name ".env*" -print'
Invoke-RestMethod http://127.0.0.1:8060/api/health | ConvertTo-Json -Compress
```

`find`는 아무 파일도 출력하지 않아야 한다. DB 연결까지 확인할 때는 데이터를 변경하지 않는 `SELECT 1`만 사용한다.

```powershell
docker exec eu-duty-automatation /opt/conda/envs/asap_pw/bin/python -c "import asap_app; from db.db_session_manager import DbSessionManager as M; assert M.GetInstance().FetchOne('SELECT 1') == 1; print('DB_READ_ONLY_OK')"
```

환경변수는 암호화 저장소가 아니다. 실행 중에는 Docker daemon 권한을 가진 관리자가 확인할 수 있다. 검증 후 container와 호스트 변수를 제거한다.

```powershell
docker stop eu-duty-automatation
Remove-Item Env:\ASAP_DATABASE_URL
$secureDbUrl = $null
```

## Step 1~3 자원 정리 기록

2026-08-03에 다음 범위로 정리했다.

- 실행 중이던 `asap-web-step3` container를 중지했다. `--rm`으로 생성됐으므로 자동 삭제됐다.
- Step 1과 Step 2 container는 이미 `--rm` 정책으로 삭제된 상태였다.
- 사용자 PostgreSQL container `ASAP`은 이 실습 산출물이 아니므로 보존했다.
- 최종 image `asap-web:step2`를 `eu-duty-automatation:local`로 다시 태그했다.
- `asap-web:step1`, `asap-web:local`, `asap-web-build:local`, `asap-web:step2` 태그를 제거했다.
- 기존 CUDA image ID `7a59b76a2c8f`의 측정값은 disk usage 13.4GB, content size 4.23GB였다.
- `pytorch-cpu=2.13.0`으로 다시 build하고 `torch.version.cuda is None`, `nvidia-*` package 없음, 주요 module import, `/api/health` 응답을 확인했다.
- 검증한 CPU image를 `eu-duty-automatation:local`로 승격하고 기존 CUDA image를 삭제했다.
- 현재 image ID는 `6872f86ee289`, disk usage 7.68GB, content size 1.88GB다.

현재 상태 확인:

```powershell
docker ps -a --format "Name={{.Names}} Image={{.Image}} Status={{.Status}}"
docker image ls eu-duty-automatation:local
```

특정 실습 container만 제거할 때는 전체 prune 대신 이름을 지정한다.

```powershell
docker stop eu-duty-automatation
docker rm eu-duty-automatation
```

이미 `--rm`으로 삭제됐다면 `No such container`는 정상이다. `docker container prune`은 이 프로젝트와 무관한 중지 container까지 삭제하므로 사용하지 않는다.

## 면접 시연용 Cloudflare Tunnel

### 경계

기존 application image는 변경하지 않는다. `compose.cloudflare-demo.yml`이 검증된 `eu-duty-automatation:local`과 공식 `cloudflare/cloudflared` image를 같은 Docker network에서 실행한다.

```text
Browser -> Cloudflare Access -> named Tunnel -> cloudflared -> app:8060
                                             -> Gunicorn/Flask -> React + API
```

익명 `trycloudflare.com` Quick Tunnel은 Server-Sent Events를 지원하지 않는다. 현재 UI는 `/api/runs/{job_id}/events`의 SSE에 의존하고 polling fallback이 없으므로 Quick Tunnel을 사용하지 않는다. Cloudflare에서 관리하는 domain, named tunnel, public hostname, Access policy가 필요하다.

Cloudflare dashboard에서 public hostname의 origin service를 `http://app:8060`으로 설정한다. 현재 API에는 application 인증이 없으므로 허용할 면접관 이메일만 Cloudflare Access policy에 등록한 뒤 tunnel을 시작한다.

### 시작

PowerShell에 DB와 현재 profile이 사용하는 LLM key를 먼저 설정한다. 파일에는 저장하지 않는다. Tunnel token도 숨김 입력으로 현재 shell에만 둔다.

```powershell
$secureToken = Read-Host "Cloudflare tunnel token" -AsSecureString
$env:CLOUDFLARE_TUNNEL_TOKEN = [Net.NetworkCredential]::new("", $secureToken).Password
docker compose -f compose.cloudflare-demo.yml pull tunnel
docker compose -f compose.cloudflare-demo.yml up -d
```

상태와 local health를 확인한다.

```powershell
docker compose -f compose.cloudflare-demo.yml ps
docker compose -f compose.cloudflare-demo.yml logs tunnel --tail 50
Invoke-RestMethod http://127.0.0.1:8060/api/health | ConvertTo-Json -Compress
```

공개 hostname에서 로그인 후 UI를 열고 실제 Pipeline event가 순차 표시되는지 확인한다. 단순 health 응답만으로 SSE 통과를 판정하지 않는다.

### 종료

```powershell
docker compose -f compose.cloudflare-demo.yml down
Remove-Item Env:\CLOUDFLARE_TUNNEL_TOKEN
$secureToken = $null
```

`down`은 `eu-duty-automatation:local` image를 삭제하지 않는다. 시연 후 Cloudflare dashboard에서 tunnel token을 rotate하거나 tunnel을 삭제한다. Token을 Git, Compose 파일, `.env` 또는 명령행 인자로 기록하지 않는다.

## EC2 Free Tier 수동 배포

### 목적

다음 단계는 image 파일을 EC2에 `scp`하는 것이 아니다. 로컬 image를 Amazon ECR에 push하고 EC2가 ECR에서 pull하게 한다.

```text
개발 PC → ECR private repository → EC2 → Docker container → port 8060
```

ECR은 image 배포 저장소이고 EC2는 image를 실행하는 원격 Linux machine이다. EC2 IAM Role에 ECR pull 권한을 주며 장기 AWS access key를 서버 파일에 저장하지 않는다.

### 현재 로컬 준비 상태

2026-08-03 AWS CLI v2 설치를 완료했다. 확인된 버전은 `aws-cli/2.36.14`, 실행 환경은 Windows 11 AMD64다. 설치 전에 열려 있던 PowerShell은 이전 `PATH`를 유지할 수 있으므로 새 창에서 확인한다.

```powershell
aws --version
aws login --profile eu-duty-dev
aws sts get-caller-identity --profile eu-duty-dev
```

최초 `sts get-caller-identity`는 `NoCredentials`였고, 이후 사용자가 `aws login`과 인증 확인 성공을 보고했다. 계정 ID와 ARN은 문서에 기록하거나 추가 조회하지 않았다. 개인 개발 계정은 [브라우저 기반 `aws login`](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html)으로 임시 자격 증명을 사용한다. 조직의 IAM Identity Center가 이미 있다면 `aws configure sso`를 대신 사용한다. access key를 repository나 Docker image에 저장하지 않는다.

### 현재의 중요한 제한

현재 CPU image는 `linux/amd64`, 7.68GB이고 압축 content는 1.88GB다. ECR private repository의 기본 Free Tier 저장량은 신규 고객 기준 월 500MB이므로 CUDA를 제거한 뒤에도 무료 저장 범위를 넘는다. ARM64 기반 `t4g` instance에는 그대로 실행할 수 없으므로 x86_64 계열을 선택해야 한다. `t3.micro`는 2 vCPU와 1GiB RAM이므로 전체 OCR·Torch·LLM pipeline 실행 대상으로는 부족하다. EC2 Free Tier에서는 우선 다음만 검증한다.

- ECR push와 EC2 pull
- Gunicorn 시작 여부
- React 정적 UI 응답
- `/api/health`
- Secret 주입 경계

전체 OCR 및 분류 실행을 Free Tier에서 보장하지 않는다. CPU-only Torch 전환은 완료했지만 EC2 배포 전에 실제 memory 사용량을 측정해야 한다. AWS Free Tier 조건은 계정 생성일에 따라 다르므로 콘솔에서 `Free tier eligible` 표시를 확인한다. 사용자는 AWS Budget 설정을 완료했다. 2025-07-15 이후 계정은 일반적으로 6개월 또는 credit 소진 시점까지이고, 이전 계정은 별도 12개월 규칙이 적용될 수 있다. ECR 저장 비용은 [공식 ECR 가격](https://aws.amazon.com/ecr/pricing/)을 기준으로 확인한다.

### EC2 단계의 완료 기준

- ECR repository `eu-duty-automatation` 생성
- commit SHA 또는 명시적 version tag로 image push
- EC2 IAM Role로 image pull
- 실제 비밀값을 image와 Git에 넣지 않고 runtime 주입
- 제한된 Security Group에서 health/UI 확인
- 사용하지 않을 때 instance 중지 또는 종료

공식 절차는 [Amazon ECR image push](https://docs.aws.amazon.com/AmazonECR/latest/userguide/docker-push-ecr-image.html)와 [EC2 Free Tier 확인](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-free-tier-usage.html)을 기준으로 한다.

## GitHub Actions 단계

수동 EC2 배포가 성공한 뒤 같은 명령을 자동화한다.

Pull Request workflow:

```text
Python compile/test → React test/build → Docker build
```

main 또는 release workflow:

```text
GitHub OIDC로 AWS 인증 → ECR push → EC2 배포 → health 확인
```

고정 AWS access key 대신 GitHub OIDC와 최소 권한 IAM Role을 사용한다. image는 `latest`만 쓰지 않고 commit SHA로 식별한다. 테스트 결과는 먼저 GitHub Actions artifact로 보존하고, 실제 요구가 확인된 뒤에만 DB 저장을 추가한다.

## Kubernetes 단계

EC2 단일 container 배포와 GitHub Actions가 안정된 뒤 Kubernetes로 이동한다.

최초 구성은 다음으로 제한한다.

- `Deployment` replica 1개
- `Service` 1개
- `/api/health` readiness/liveness probe
- DB 및 LLM 값의 runtime Secret 주입
- 비밀이 아닌 TOML 설정용 ConfigMap
- 필요한 산출물만 PVC 또는 object storage로 외부화

현재 `RunRegistry`와 background job은 process-local이므로 replica를 늘리지 않는다. 다중 Pod와 rolling update를 안전하게 사용하려면 job state, event buffer, artifact, worker queue를 먼저 외부 저장소로 옮겨야 한다.

## 다음 실행 작업

1. `tests/test_question_answer_replay.py`로 질문 중단·응답 재실행 계약을 검증한다.
2. ECR repository를 만들고 CPU image를 push한다.
3. EC2에서 image를 pull해 health와 UI를 수동 검증한다.
4. 검증된 수동 명령만 GitHub Actions로 옮긴다.
5. 마지막에 Docker Desktop Kubernetes와 EKS 순서로 확장한다.
