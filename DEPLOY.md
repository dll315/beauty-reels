# 部署方案

按场景选一种即可。所有方案共用仓库里的 `index.html` + `server.py`，无第三方依赖（Python 3.7+ 标准库即可运行）。

> **公网部署安全提示**：`/api/resolve` 会替访问者调用免费视频 API。部署到公网时请设置 `ACCESS_TOKEN` 口令（各方案内均有说明），并把页面地址以 `?key=口令` 访问一次，浏览器会记住。

---

## 方案一：Docker（推荐 · VPS / NAS / 群晖均适用）

```bash
git clone https://github.com/dll315/beauty-reels.git
cd beauty-reels

# 方式 A：docker compose（推荐，自带重启策略）
docker compose up -d --build

# 方式 B：纯 docker
docker build -t beauty-reels .
docker run -d --name beauty-reels -p 8899:8899 --restart unless-stopped beauty-reels

# 公网部署加访问口令：
docker run -d --name beauty-reels -p 8899:8899 --restart unless-stopped \
  -e ACCESS_TOKEN=你的口令 beauty-reels
```

访问 `http://服务器IP:8899/?key=你的口令`（设置了口令时；首次带 key 访问后浏览器自动记住）。

更新版本：

```bash
cd beauty-reels && git pull
docker compose up -d --build
```

镜像基于 `python:3.12-slim`，体积约 50MB，内存占用 < 50MB，NAS / 树莓派（arm64 镜像自动拉取）都能跑。

---

## 方案二：免费云平台一键部署（Render / Railway / Koyeb）

仓库自带 `Dockerfile`，这类平台会自动识别：

1. 把仓库 Fork / 推送到你自己的 GitHub
2. 平台控制台 → New Web Service → 选择该仓库 → 类型选 **Docker** → 部署
3. 环境变量里设置 `ACCESS_TOKEN=你的口令`
4. 部署完成后用平台分配的域名访问（带 `?key=口令`）

注意：Render 免费档 15 分钟无访问会休眠，首次打开要等 ~30 秒冷启动；Railway 免费额度按用量计。

---

## 方案三：VPS 裸机 + systemd（不想到处搬 Docker 时）

```bash
git clone https://github.com/dll315/beauty-reels.git /opt/beauty-reels
cd /opt/beauty-reels
```

创建 `/etc/systemd/system/beauty-reels.service`：

```ini
[Unit]
Description=Beauty Reels (random short video)
After=network.target

[Service]
WorkingDirectory=/opt/beauty-reels
ExecStart=/usr/bin/python3 server.py --host 0.0.0.0 --port 8899 --no-open
Environment=ACCESS_TOKEN=你的口令
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now beauty-reels
```

需要域名 + HTTPS 时，前面挂一层 Nginx 反代 `127.0.0.1:8899` 即可（纯静态+一个 JSON 接口，无 WebSocket，无特殊配置）。

---

## 方案四：本地 Windows 常驻（家庭电脑当服务器）

1. 双击 `启动网站.bat`（本机使用），或让局域网设备访问：
   `python server.py --host 0.0.0.0 --port 8899 --no-open`
2. 开机自启（管理员 PowerShell 一次性执行）：

```powershell
schtasks /create /tn "BeautyReels" /tr "D:\Zcodeworks\beauty-reels\启动网站.bat" /sc onstart /rl highest
```

> 局域网访问需在 Windows 防火墙放行 8899 端口。

---

## 方案五：纯静态托管（GitHub Pages / Cloudflare Pages）——受限模式

`index.html` 是单文件，把它单独丢到任意静态托管即可观看（直连模式）。

- 优点：零服务器、零成本
- 限制：浏览器 CORS 限制导致**收藏 / 历史 / 下载不可用**（这些功能依赖本地服务解析直链），会有相应提示

GitHub Pages 发布方法：

```bash
git clone https://github.com/dll315/beauty-reels.git
cd beauty-reels
git checkout --orphan site
git rm -rf . && git checkout main -- index.html
git commit -m "static site" && git push origin site
# 仓库 Settings → Pages → 分支选 site
```

---

## 方案对比

| 方案 | 成本 | 完整功能 | 适合 |
| --- | --- | --- | --- |
| Docker | VPS/NAS 电费 | ✅ 全部 | 长期自用、家用 NAS |
| 云平台 | 免费档（会休眠） | ✅ 全部 | 不想管服务器 |
| VPS systemd | VPS | ✅ 全部 | 已有服务器 |
| 本地 Windows | 电费 | ✅ 全部 | 只在家里/局域网看 |
| 静态托管 | 免费 | ⚠️ 仅观看 | 白嫖党、随手分享 |
