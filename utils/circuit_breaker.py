"""
╔══════════════════════════════════════════════════════════════════════╗
║                    MelomaniacPass v5.0                               ║
║                  Patrón Circuit Breaker                              ║
╚══════════════════════════════════════════════════════════════════════╝

Módulo: utils/circuit_breaker.py
Descripción: Implementa el patrón Circuit Breaker para protección contra
            rate limiting de APIs externas. Detecta errores HTTP 429 y
            desactiva temporalmente las peticiones a la plataforma afectada,
            notificando a la UI para mostrar countdown en tiempo real.

Componentes:
    - RateLimitError: Excepción personalizada para rate limiting
    - CircuitBreaker: Implementación del patrón con auto-reset y notificaciones

Estrategia de Diseño:
    El circuit breaker actúa como fusible de protección entre la aplicación
    y las APIs externas. Cuando detecta un HTTP 429 (Too Many Requests):
    1. Abre el circuito (is_open=True) bloqueando nuevas peticiones
    2. Notifica a todos los suscriptores (UI) del estado y tiempo restante
    3. Inicia un temporizador de auto-reset asíncrono
    4. Cierra el circuito automáticamente tras el cooldown
    
    Esto previene cascadas de errores y mejora la experiencia del usuario
    con feedback visual del tiempo de espera.

Autor: MelomaniacPass Team
Versión: 5.0
Fecha: 2026
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable, Optional


class SpotifyBanException(Exception):
    """
    Baneo activo de Spotify (HTTP 429). Aborta la transferencia inmediatamente.

    Se lanza cuando Spotify devuelve un 429 a pesar de las protecciones del
    SpotifyRateLimiter. Es un error fatal: no se reintenta, se cancela la
    operación y se informa al usuario con un mensaje [FATAL].

    Attributes:
        retry_after: Segundos indicados por Spotify en el header Retry-After.
    """
    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"Spotify ban activo. Retry-After: {int(retry_after)}s")


class RateLimitError(Exception):
    """
    Excepción lanzada cuando una plataforma está en estado de rate limiting.
    
    Encapsula información sobre la plataforma afectada y el tiempo de espera
    requerido antes de reintentar peticiones.
    
    Attributes:
        platform: Nombre de la plataforma que impuso el rate limit
                 ("spotify", "youtube", "apple").
        retry_after: Segundos que deben transcurrir antes de reintentar.
    
    Note:
        Esta excepción es capturada por capas superiores para mostrar
        mensajes informativos al usuario y deshabilitar controles de UI
        temporalmente.
    """
    def __init__(self, platform: str, retry_after: int):
        self.platform    = platform
        self.retry_after = retry_after
        super().__init__(f"Rate-limited by {platform}. Retry in {retry_after}s.")


class CircuitBreaker:
    """
    Implementación del patrón Circuit Breaker para protección contra rate limiting.
    
    Monitorea el estado de conectividad con APIs externas y previene cascadas
    de errores al detectar condiciones de rate limiting (HTTP 429). Cuando se
    detecta un 429, el breaker "se abre" bloqueando nuevas peticiones y
    notificando a suscriptores (típicamente la UI) para mostrar feedback visual.
    
    El circuito se cierra automáticamente tras un período de cooldown, permitiendo
    que las peticiones se reanuden sin intervención manual.
    
    Attributes:
        platform: Nombre de la plataforma protegida por este breaker.
        default_cooldown: Tiempo de espera por defecto en segundos (60s).
        is_open: Estado del circuito. True = bloqueado, False = operativo.
    
    Methods:
        subscribe: Registra un callback para notificaciones de cambio de estado.
        trip: Abre el circuito manualmente (típicamente tras detectar HTTP 429).
        check_or_raise: Verifica el estado y lanza RateLimitError si está abierto.
        remaining: Propiedad que retorna segundos restantes hasta el reset.
    
    Example:
        >>> breaker = CircuitBreaker("spotify", default_cooldown=60)
        >>> breaker.subscribe(lambda is_open, remaining: print(f"Open: {is_open}"))
        >>> breaker.trip(retry_after=120)  # Abre por 120 segundos
        >>> breaker.check_or_raise()  # Lanza RateLimitError
        RateLimitError: Rate-limited by spotify. Retry in 120s.
    
    Note:
        El patrón Circuit Breaker es esencial para aplicaciones que consumen
        APIs con rate limits estrictos. Previene:
        - Cascadas de errores 429 que degradan la experiencia del usuario
        - Bloqueos temporales más largos por reintentos agresivos
        - Consumo innecesario de cuota de API durante períodos de throttling
    """
    
    def __init__(self, platform: str, default_cooldown: int = 60):
        """
        Inicializa un nuevo Circuit Breaker para una plataforma específica.
        
        Args:
            platform: Nombre de la plataforma a proteger.
            default_cooldown: Tiempo de espera por defecto en segundos.
        """
        self.platform         = platform
        self.default_cooldown = default_cooldown
        self.is_open: bool    = False
        self._until: float    = 0.0
        self._callbacks: list[Callable[[bool, int], None]] = []

    def subscribe(self, cb: Callable[[bool, int], None]) -> None:
        """
        Registra un callback para recibir notificaciones de cambio de estado.
        
        El callback será invocado cuando el circuito se abra o cierre,
        recibiendo el nuevo estado y el tiempo restante hasta el reset.
        
        Args:
            cb: Función callback con firma (is_open: bool, remaining: int) -> None.
        
        Note:
            Los callbacks son típicamente usados por la UI para actualizar
            el estado visual de botones y mostrar countdowns en tiempo real.
        """
        self._callbacks.append(cb)

    def trip(self, retry_after: Optional[int] = None) -> None:
        """
        Abre el circuito manualmente, bloqueando nuevas peticiones.
        
        Este método es invocado cuando se detecta un HTTP 429 o cualquier
        otra condición que requiera pausar las peticiones a la plataforma.
        Notifica a todos los suscriptores y programa un auto-reset.
        
        Args:
            retry_after: Segundos de espera antes del reset. Si es None,
                        usa default_cooldown.
        
        Note:
            El auto-reset es manejado por una tarea asyncio independiente
            que cierra el circuito automáticamente tras el cooldown,
            sin requerir intervención manual del usuario.
        """
        wait         = retry_after or self.default_cooldown
        self.is_open = True
        self._until  = time.monotonic() + wait
        self._notify(True, wait)
        asyncio.create_task(self._auto_reset(wait))

    def check_or_raise(self) -> None:
        """
        Verifica el estado del circuito y lanza excepción si está abierto.
        
        Este método debe ser invocado antes de cada petición a la API
        para garantizar que el circuito está cerrado. Si está abierto,
        lanza RateLimitError con el tiempo restante.
        
        Raises:
            RateLimitError: Si el circuito está abierto (is_open=True).
        
        Note:
            Este patrón de "check antes de ejecutar" es preferible a
            intentar la petición y manejar el error, ya que evita
            latencia innecesaria y consumo de recursos.
        """
        if self.is_open:
            raise RateLimitError(self.platform, int(self.remaining))

    @property
    def remaining(self) -> float:
        """
        Calcula y retorna los segundos restantes hasta el auto-reset.
        
        Returns:
            Segundos restantes (float). Retorna 0.0 si el circuito está cerrado
            o el tiempo ya expiró.
        
        Note:
            Usa time.monotonic() en lugar de time.time() para garantizar
            precisión incluso si el reloj del sistema es ajustado durante
            el cooldown.
        """
        return max(0.0, self._until - time.monotonic())

    def _notify(self, is_open: bool, remaining: int) -> None:
        """
        Notifica a todos los callbacks registrados del cambio de estado.
        
        Args:
            is_open: Nuevo estado del circuito.
            remaining: Segundos restantes hasta el reset.
        
        Note:
            Los errores en callbacks individuales son capturados y silenciados
            para prevenir que un callback defectuoso afecte a otros suscriptores
            o interrumpa el flujo del circuit breaker.
        """
        for cb in self._callbacks:
            try:
                cb(is_open, remaining)
            except Exception:  # pylint: disable=broad-exception-caught
                pass

    async def _auto_reset(self, wait: float) -> None:
        """
        Tarea asíncrona que cierra el circuito automáticamente tras el cooldown.
        
        Args:
            wait: Segundos a esperar antes de cerrar el circuito.
        
        Note:
            Esta tarea se ejecuta en background sin bloquear el event loop.
            Al finalizar, cierra el circuito y notifica a todos los suscriptores
            que las peticiones pueden reanudarse.
        """
        await asyncio.sleep(wait)
        self.is_open = False
        self._notify(False, 0)
