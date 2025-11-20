from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles # Para servir arquivos estáticos
from typing import Dict, List, Optional, Any
from game_manager import game_manager, Game, GameConfig, MIN_PLAYERS # Importa GameConfig
import json
import uuid
import asyncio # Para o loop do temporizador

# Inicialização
app = FastAPI()

# Para servir o index.html e outros arquivos estáticos (CSS, JS se houverem)
# Certifique-se que 'index.html' esteja na mesma pasta do main_server.py
# ou crie uma pasta 'static' e monte ela. Por enquanto, assumimos na raiz.
# @app.get("/") já serve o index.html diretamente.

# Armazena as conexões WebSocket ativas para cada jogo
active_connections: Dict[str, List[WebSocket]] = {} 

# --- FUNÇÕES AUXILIARES DE BROADCAST ---

async def broadcast_game_state(game_id: str):
    """Envia o estado público atual do jogo para todos os jogadores conectados."""
    game = game_manager.get_game(game_id)
    if not game:
        if game_id in active_connections: # Se o jogo foi removido, fecha as conexões
            for connection in active_connections[game_id]:
                try:
                    await connection.close(code=1011, reason="Jogo encerrado pelo host.")
                except RuntimeError: # Conexão já fechada
                    pass
            del active_connections[game_id]
        return

    state = game.get_public_state()
    message = json.dumps({"type": "STATE_UPDATE", "data": state})
    
    connections_to_remove = []
    for connection in active_connections.get(game_id, []):
        try:
            await connection.send_text(message)
        except RuntimeError: # Conexão fechada
            connections_to_remove.append(connection)
            
    for connection in connections_to_remove:
        active_connections[game_id].remove(connection)

async def send_private_message(websocket: WebSocket, data: Dict):
    """Envia uma mensagem privada (palavra/papel) por WebSocket para um cliente específico."""
    message = json.dumps({"type": "PRIVATE_MESSAGE", "data": data})
    try:
        await websocket.send_text(message)
    except RuntimeError: # Conexão fechada
        pass

# --- ROTAS HTTP (Criação e Informação) ---

@app.get("/", response_class=HTMLResponse)
async def get_home():
    # Isso servirá o arquivo HTML do Frontend
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            html_content = f.read()
        return HTMLResponse(content=html_content)
    except FileNotFoundError:
        return HTMLResponse("<h1>Página Inicial</h1><p>Frontend (index.html) não encontrado. Crie o arquivo index.html no mesmo diretório.</p>", status_code=404)

@app.post("/api/create_game/{player_name}")
async def create_game(player_name: str, request: Request):
    """Cria um novo jogo com configurações e retorna o ID."""
    # Extrai as configurações do corpo da requisição
    body = await request.json()
    config = GameConfig(
        clue_time=body.get("clue_time", 60),
        vote_time=body.get("vote_time", 90),
        rounds_per_player=body.get("rounds_per_player", 1)
    )

    player_id = str(uuid.uuid4())
    game = game_manager.create_game(player_id, player_name, config)
    
    return {
        "game_id": game.game_id, 
        "host_id": player_id, 
        "message": f"Jogo criado com sucesso. ID: {game.game_id}"
    }

# --- ROTA WEBSOCKET (Comunicação em Tempo Real) ---

@app.websocket("/ws/{game_id}/{player_id}/{player_name}")
async def websocket_endpoint(websocket: WebSocket, game_id: str, player_id: str, player_name: str):
    game = game_manager.get_game(game_id)
    if not game:
        await websocket.close(code=1008, reason="Jogo não encontrado.")
        return

    await websocket.accept()
    
    if game_id not in active_connections:
        active_connections[game_id] = []
        
    active_connections[game_id].append(websocket)

    # Tenta adicionar o jogador, se não for o host reconectando
    is_new_player = game.add_player(player_id, player_name)

    print(f"Nova conexão em {game_id}: {player_name} (ID: {player_id})")
    await broadcast_game_state(game_id)

    try:
        while True:
            data = await websocket.receive_text()
            
            try:
                message = json.loads(data)
                command = message.get("command")
                payload = message.get("payload", {})
            except json.JSONDecodeError:
                await send_private_message(websocket, {"type": "ERROR", "message": "Formato de mensagem JSON inválido."})
                continue

            response: Dict[str, Any] = {"type": "ERROR", "message": "Comando inválido ou sem permissão."}

            if command == "START_GAME":
                if player_id == game.host_id:
                    start_result = game.start_game()
                    if "success" in start_result:
                        # Envia palavras privadas por WS para cada jogador
                        for p_id, p_data in start_result['private_words_data'].items():
                            player_ws = next((conn for conn in active_connections.get(game_id, []) if conn.scope['path'].split('/')[3] == p_id), None)
                            if player_ws:
                                await send_private_message(player_ws, {"type": "PRIVATE_MESSAGE", "data": p_data})
                        response = {"type": "GAME_STARTED"}
                    else:
                         response = {"type": "ERROR", "message": start_result["error"]}
                else:
                    response = {"type": "ERROR", "message": "Apenas o Host pode iniciar o jogo."}
            
            elif command == "SUBMIT_CLUE":
                clue = payload.get("clue")
                result = game.submit_clue(player_id, clue)
                if "success" in result:
                    response = {"type": "CLUE_ACCEPTED"}
                else:
                    response = {"type": "ERROR", "message": result["error"]}
            
            elif command == "VOTE":
                voted_id = payload.get("voted_id") # Note: o frontend envia o player.id agora
                result = game.submit_vote(player_id, voted_id)
                
                if "success" in result or result.get("status") == "GAME_OVER":
                    if result.get("status") == "GAME_OVER":
                        response = {"type": "GAME_OVER", "results": result["results"]}
                        # Não remove o jogo aqui, deixa o ping do temporizador fazer isso,
                        # ou um comando de reset/limpeza do host.
                    else:
                        response = {"type": "VOTE_ACCEPTED"}
                else:
                    response = {"type": "ERROR", "message": result["error"]}
            
            elif command == "GET_PRIVATE_DATA": # Permite ao cliente pedir a palavra privada se reconectar
                private_data = game.get_private_player_data(player_id)
                if private_data:
                    await send_private_message(websocket, {"type": "PRIVATE_MESSAGE", "data": private_data})
                    response = {"type": "INFO", "message": "Dados privados enviados."}
                else:
                    response = {"type": "ERROR", "message": "Dados privados não disponíveis."}
            
            # Não envia resposta para broadcast, apenas para o cliente que enviou o comando
            if response["type"] != "INFO": # Evita double-send para private_data
                await send_private_message(websocket, response)
            
            # Sempre broadcasta o estado após um comando que muda o jogo
            if command in ["START_GAME", "SUBMIT_CLUE", "VOTE"]:
                await broadcast_game_state(game_id)

    except WebSocketDisconnect:
        print(f"Desconexão em {game_id}: {player_name} (ID: {player_id})")
        if websocket in active_connections.get(game_id, []):
            active_connections[game_id].remove(websocket)
        
        # Se o host sair, o jogo é fechado e limpo
        if player_id == game.host_id:
            game_manager.remove_game(game_id)
            print(f"Host {player_name} saiu, jogo {game_id} removido.")
            await broadcast_game_state(game_id) # Notifica que o jogo sumiu
        elif game.status == "WAITING_FOR_PLAYERS":
            game.remove_player(player_id)
            await broadcast_game_state(game_id)

@app.on_event("startup")
async def startup_event():
    # Inicia um loop em background para verificar temporizadores
    asyncio.create_task(game_timer_loop())

async def game_timer_loop():
    while True:
        await asyncio.sleep(1) # Verifica a cada segundo
        games_to_remove = []
        for game_id, game in game_manager.active_games.items():
            result = game.check_timer()
            if result:
                if result.get("status") == "GAME_OVER":
                    await broadcast_game_state(game_id)
                    # Dá um tempo para o frontend exibir o resultado antes de remover
                    await asyncio.sleep(10) 
                    games_to_remove.append(game_id)
                elif result.get("event"):
                    await broadcast_game_state(game_id)
        
        for game_id in games_to_remove:
            game_manager.remove_game(game_id)
            await broadcast_game_state(game_id) # Envia o último estado (jogo removido)