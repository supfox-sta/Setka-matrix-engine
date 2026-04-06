# Архитектура единой сети "Сетка"

## Компоненты инфраструктуры

### 1. Core Network (Mesh Network)
```
Ноутбук (512GB) - Central Hub
├── Сервер Казань (Remote Node)
├── Сервер Питер (Remote Node)
└── Mesh VPN (WireGuard/Tailscale)
```

### 2. Сервисы на центральном хабе (ноутбук)
- **Matrix Synapse** (уже есть) - основа OAuth
- **OAuth Provider** - единая авторизация
- **Web Server** - магазины и сайты
- **Game Server** - Unity мультиплеер
- **Blockchain Node** - блокчейн инфраструктура
- **API Gateway** - прокси сервисов

### 3. Удаленные серверы
- **Казань**: Backup, CDN, Game Server Instance
- **Питер**: Blockchain Node, Web Services, Storage

## Технический стек

### Сетевой уровень
- **WireGuard** - приватная mesh сеть
- **Tailscale** - управление сетью
- **Docker Swarm** - оркестрация

### OAuth через Сетку
```
Client Apps → OAuth (Matrix) → Services
├── Web Магазин
├── Игровой Магазин (Steam-like)
├── Unity Game Server
├── Blockchain Wallet
└── Admin Panels
```

### Домены
- `setka-matrix.ru` - Matrix/OAuth
- `shop.setka-matrix.ru` - магазин
- `games.setka-matrix.ru` - игровой магазин
- `blockchain.setka-matrix.ru` - блокчейн
- `api.setka-matrix.ru` - API шлюз

## Реализация

### Шаг 1: Mesh Network
```bash
# Установка WireGuard
sudo apt install wireguard

# Настройка центрального хаба
wg genkey | tee privatekey | wg pubkey > publickey
```

### Шаг 2: OAuth Provider
На базе существующего Matrix Synapse:
- Расширить OIDC endpoints
- Добавить client management
- Создать admin panel

### Шаг 3: Web Services
- FastAPI для API
- React для фронтенда
- PostgreSQL для данных

### Шаг 4: Game Server
- Unity Mirror/Netcode
- Docker контейнеризация
- Балансировка нагрузки

### Шаг 5: Blockchain
- Ethereum-based private chain
- IPFS для хранения
- Web3.js интеграция
