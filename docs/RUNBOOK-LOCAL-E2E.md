# Runbook — Local mode E2E (1 server + 2 provider `--at` engine ngoài)

Test tay end-to-end **local mode** (không control-plane, không login): build `grid` từ code, dựng 1
grid local với **1 signaling server** + **2 provider** cùng trỏ tới một gateway OpenAI-compatible bên
ngoài, rồi gọi thử qua endpoint của grid.

- **Server** = `grid up` (uvicorn signaling, cổng `:8090`) — sổ đăng ký engine + proxy `/v1/*`.
- **Provider** = `grid join --at <url>` — mỗi cái là 1 tiến trình `__engine` heartbeat, **không** chạy
  model tại chỗ mà forward tới gateway ngoài. 2 provider ⇒ server load-balance giữa 2 node.
- **Upstream** = `https://vibe-agent-gateway.eternalai.org/v2`, model `deepreinforce-ai/ornith-1.0-397b`.

> Vì engine ở ngoài (`--at`), **không cần** `grid engine install` hay `grid pull` — bỏ qua tải model.

```
   grid chat / OpenAI SDK
            │  POST /v1/chat/completions
            ▼
   ┌──────────────────┐   _choose_engine (load-balance)
   │  __server :8090  │──────────────┬───────────────┐
   └──────────────────┘              │               │
                                     ▼               ▼
                              __engine p1      __engine p2
                                     │               │
                                     └──────┬────────┘
                                            ▼  forward /chat/completions
                          https://vibe-agent-gateway.eternalai.org/v2
                                 model: deepreinforce-ai/ornith-1.0-397b
```

---

## 0. Chuẩn bị: build `grid` từ code

```bash
cd /Users/dudu/Bitcoin_builder/Grid/autonomous-grid
uv tool install . --reinstall --no-cache      # cài global `grid` từ source hiện tại
grid version                                  # xác nhận bản vừa build
```

> Không có `uv`? Dùng editable: `pip install -e .` rồi gọi `grid`, hoặc chạy thẳng `python -m cli …`
> trong repo (thay `grid` bằng `python -m cli` ở mọi lệnh dưới).

Trước khi dựng grid, **kiểm tra gateway gọi được không cần key** (xem cảnh báo auth ở mục Ghi chú):

```bash
curl -sS https://vibe-agent-gateway.eternalai.org/v2/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"deepreinforce-ai/ornith-1.0-397b","messages":[{"role":"user","content":"hi"}],"max_tokens":16}'
```

Trả về JSON completion ⇒ OK. Trả về `401/403` ⇒ gateway cần API key, local proxy **không** forward
key nên phải xử lý trước (xem Ghi chú) rồi mới chạy tiếp.

---

## 1. Terminal 1 — Server (`grid up`)

Dùng `GRID_HOME` riêng ở `/tmp` để cô lập hẳn với `~/.grid` thật và dễ dọn. Cả server lẫn 2 provider
trong runbook này **chung một `GRID_HOME`** (cùng một máy, cùng một grid) — nên chạy trong cùng shell,
hoặc `export GRID_HOME=/tmp/grid-local` ở mỗi terminal.

```bash
export GRID_HOME=/tmp/grid-local
rm -rf "$GRID_HOME"                 # sạch trạng thái cũ

grid mode local                    # ghi state.json ← local (nhớ cho các lệnh sau)
grid up                            # tạo grid "home" + bật __server trên :8090
# → in ra:  grid=home
#           grid_url=http://<lan-ip>:8090
```

Kiểm tra server sống:

```bash
grid ls                            # thấy home ... local ... http://<ip>:8090
curl -s http://127.0.0.1:8090/grid/info | python3 -m json.tool
```

---

## 2. Terminal 2 — Provider 1 (`--at`)

```bash
export GRID_HOME=/tmp/grid-local

grid join \
  --at https://vibe-agent-gateway.eternalai.org/v2 \
  -m deepreinforce-ai/ornith-1.0-397b \
  --advertise-as deepreinforce-ai/ornith-1.0-397b \
  --ctx-size 200000 \
  --name p1
# → Engine node-… advertised on http://<ip>:8090
#   models=deepreinforce-ai/ornith-1.0-397b
```

`--name p1` là engine id (bắt buộc phân biệt với provider 2). `--advertise-as` = tên model mà grid
quảng bá; khi request tới, proxy rewrite alias→tên thật trước khi forward (ở đây trùng nhau nên không
đổi gì).

---

## 3. Terminal 3 — Provider 2 (`--at`)

Y hệt provider 1, chỉ **đổi `--name`**:

```bash
export GRID_HOME=/tmp/grid-local

grid join \
  --at https://vibe-agent-gateway.eternalai.org/v2 \
  -m deepreinforce-ai/ornith-1.0-397b \
  --advertise-as deepreinforce-ai/ornith-1.0-397b \
  --ctx-size 200000 \
  --name p2
```

> 2 engine cùng model, cùng upstream ⇒ `_choose_engine` chọn theo `load` rồi `last_heartbeat` (ưu
> tiên node rảnh / heartbeat cũ nhất), tạo hiệu ứng luân phiên giữa p1 và p2.

---

## 4. Kiểm tra & E2E

```bash
export GRID_HOME=/tmp/grid-local

grid engines                       # phải thấy 2 engine: p1, p2 (đều live)
grid models --verbose              # model ornith-1.0-397b, cột engine hiện p1 & p2
grid info                          # engines=2, models=deepreinforce-ai/ornith-1.0-397b
```

Gọi qua CLI:

```bash
grid chat -m deepreinforce-ai/ornith-1.0-397b "viết 1 câu chào ngắn"
```

Gọi thẳng endpoint OpenAI của grid (đúng cách app thật dùng):

```bash
source <(grid info --env)          # export OPENAI_BASE_URL=http://<ip>:8090/v1 , OPENAI_API_KEY=local-grid
curl -sS "$OPENAI_BASE_URL/chat/completions" \
  -H 'content-type: application/json' \
  -d '{"model":"deepreinforce-ai/ornith-1.0-397b","messages":[{"role":"user","content":"hi"}],"max_tokens":32}' \
  | python3 -m json.tool
```

Chạy lệnh trên vài lần rồi soi access log để thấy request được nhận (và, gián tiếp, phân phối cho 2
engine):

```bash
tail -f "$GRID_HOME/grids/"*/server.log          # mỗi request 1 dòng POST /v1/chat/completions 200
tail -f "$GRID_HOME/run/engines/"*/p1.log         # log của provider 1
```

---

## 5. Dọn

```bash
export GRID_HOME=/tmp/grid-local
grid leave --all                   # gỡ + unregister cả p1, p2
grid down                          # tắt server
pkill -f "__engine" ; pkill -f "__server"   # chắc ăn, dọn tiến trình còn sót
rm -rf "$GRID_HOME"                # xoá sạch (config + log)
```

---

## Ghi chú & troubleshooting

- **⚠️ Auth upstream KHÔNG được forward.** Proxy local (`local/server.py::_proxy_openai`) chỉ đặt
  header `content-type` khi forward — **không** gắn/không chuyển tiếp `Authorization`. Nên gateway
  `--at` phải chấp nhận gọi **keyless**. Nếu nó cần Bearer key thì runbook này chưa chạy được as-is:
  hoặc dùng gateway không cần key, hoặc sửa `_proxy_openai` để tiêm key (ví dụ đọc từ env) trước khi
  forward. Luôn chạy bước curl trực tiếp ở Mục 0 để loại trừ nguyên nhân này trước.
- **`--ctx-size 200000` là no-op với `--at`.** Cờ này (cùng `--n-predict`, `--parallel`, `--flash-attn`,
  `--temp`, `--reasoning-budget`) chỉ cấu hình engine **built-in** (`--serve` → llama-server). Với
  `--at` engine đã chạy sẵn ở ngoài nên chúng được lưu vào record nhưng không có tác dụng. Giữ lại
  cũng không sao; muốn ngữ cảnh 200k thì phải cấu hình ở chính gateway.
- **URL forward.** `endpoint_url` được nối `"/chat/completions"` ⇒ `--at …/v2` sẽ gọi
  `…/v2/chat/completions`. Muốn đích khác thì chỉnh phần base của `--at` cho khớp (đừng thêm/thừa
  `/v1` hay `/chat/completions`).
- **Engine "sống" theo heartbeat TTL 60s.** Cứ để tiến trình provider chạy; đừng Ctrl-C. Đóng provider
  ⇒ node rơi khỏi `grid engines`/`grid models` sau TTL.
- **Trùng `--name`** ⇒ `Engine 'p1' is already joined`. Mỗi provider một tên khác nhau.
- **Nhiều máy thật (thay vì 2 provider trên 1 máy):** để server ở máy A, provider chạy máy B/C và
  join bằng **grid_url** thay vì tên: `grid join http://<ip-máy-A>:8090 --at <url> -m <model> …`.
- **Log ở đâu / vì sao phình:** xem [local-flow-and-logging.md](local-flow-and-logging.md). Tóm tắt:
  `server.log` (rotating, mỗi HTTP request 1 dòng — heartbeat 15s/engine là nguồn tăng đều),
  `run/engines/<grid>/p{1,2}.log` (capped 50MB). Muốn giảm access log: đặt `UVICORN_LOG_LEVEL=warning`
  **trước** `grid up`.
