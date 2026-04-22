"""
╔══════════════════════════════════════════════════════════════════════╗
║                    MelomaniacPass v5.0                               ║
║              Transferencia Universal de Playlists                    ║
╚══════════════════════════════════════════════════════════════════════╝

Módulo: app.py
Descripción: Punto de entrada principal de la aplicación MelomaniacPass.
            Orquesta la inicialización de servicios, estado, UI y autenticación.
            Implementa ciclo de vida completo con limpieza profunda de recursos.

Arquitectura: BLoC-inspired (AppState ◄─ Service ◄─ UI)
Diseño: Superficies oscuras sólidas · IBM Plex Sans · Optimizado OLED
Motor: Hunter Recovery · Universal Auth · Post-mortem
Ciclo de vida: Hard exit · Session probes · Semáforo real

Autor: MelomaniacPass Team
Versión: 5.0
Fecha: 2026
"""

import asyncio
import os

import flet as ft
from dotenv import load_dotenv

from core.state import AppState
from services.api_service import MusicApiService
from ui.main_ui import PlaylistManagerUI
from auth_manager import AuthManager
from utils.circuit_breaker import CircuitBreaker

load_dotenv()

# ══════════════════════════════════════════════════════════════════════
# TOKENS DE DISEÑO
# ══════════════════════════════════════════════════════════════════════
# Paleta de colores optimizada para interfaces OLED con alto contraste
# y reducción de fatiga visual en sesiones prolongadas.

BG_LIST  = "#FF161622"      # Fondo principal de listas
ACCENT   = "#FF4F8BFF"      # Color de acento para elementos interactivos
BG_SURFACE = "#FF111118"    # Fondo de superficies elevadas
TEXT_PRIMARY = "#FFF2F6FF"  # Texto principal de alta legibilidad


async def main(page: ft.Page) -> None:
    """
    Función principal asíncrona de la aplicación MelomaniacPass.
    
    Inicializa y orquesta todos los componentes del sistema:
    - Configuración de la ventana y tema visual
    - Instanciación de servicios (APIs, estado, UI, autenticación)
    - Establecimiento de referencias bidireccionales entre componentes
    - Configuración de ciclo de vida y limpieza de recursos
    
    Args:
        page: Instancia de página Flet que representa la ventana de la aplicación.
    
    Raises:
        asyncio.CancelledError: Capturado silenciosamente durante el cierre normal.
        Exception: Cualquier excepción no manejada fuerza salida del sistema.
    
    Note:
        Esta función implementa un patrón de limpieza profunda (hard_cleanup)
        para garantizar la liberación completa de recursos del sistema operativo,
        previniendo procesos huérfanos y fugas de memoria.
    """
    try:
        # ──────────────────────────────────────────────────────────────
        # CONFIGURACIÓN DE VENTANA Y TEMA
        # ──────────────────────────────────────────────────────────────
        # Establece dimensiones, colores y comportamiento visual de la
        # ventana principal de la aplicación.
        
        page.title             = "MelomaniacPass"
        page.window.bgcolor    = BG_LIST
        page.bgcolor           = BG_LIST
        page.window.width      = 1200
        page.window.height     = 650
        page.window.min_width  = 1030
        page.window.min_height = 600
        page.padding           = 0
        page.spacing           = 0
        page.theme_mode        = ft.ThemeMode.DARK

        def _exception_handler(loop, context: dict) -> None:
            """
            Manejador personalizado de excepciones del event loop asyncio.
            
            Filtra excepciones benignas relacionadas con el cierre de conexiones
            y shutdown del sistema, evitando logs innecesarios y permitiendo
            un cierre limpio de la aplicación.
            
            Args:
                loop: Event loop de asyncio.
                context: Diccionario con información de la excepción.
            
            Note:
                Ignora ConnectionResetError, BrokenPipeError y mensajes de
                shutdown para evitar ruido en logs durante el cierre normal.
            """
            exc = context.get("exception")
            if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
                return
            msg = context.get("message", "")
            if "_call_connection_lost" in msg or "shutdown" in msg:
                return
            loop.default_exception_handler(context)

        asyncio.get_event_loop().set_exception_handler(_exception_handler)

        # ──────────────────────────────────────────────────────────────
        # CONFIGURACIÓN DE FUENTES Y TEMA
        # ──────────────────────────────────────────────────────────────
        # Carga la fuente IBM Plex Sans desde Google Fonts y configura
        # el esquema de colores del tema oscuro.
        
        page.fonts = {
            "IBM Plex Sans": (
                "https://fonts.gstatic.com/s/ibmplexsans/v19/"
                "zYXgKVElMYYaJe8bpLHnCwDKjR7_MIZs.woff2"
            ),
        }
        page.theme = ft.Theme(
            font_family="IBM Plex Sans",
            color_scheme=ft.ColorScheme(
                primary=ACCENT, surface=BG_SURFACE,
                on_primary=TEXT_PRIMARY, on_surface=TEXT_PRIMARY,
            ),
        )

        # ──────────────────────────────────────────────────────────────
        # INICIALIZACIÓN DE COMPONENTES DEL SISTEMA
        # ──────────────────────────────────────────────────────────────
        # Instancia los componentes principales siguiendo el patrón de
        # inyección de dependencias: CircuitBreakers → Service → State → UI
        
        circuit_breakers = {p: CircuitBreaker(p) for p in AppState.PLATFORMS}
        service      = MusicApiService(circuit_breakers)
        state        = AppState(service)
        ui           = PlaylistManagerUI(page, state)
        auth_manager = AuthManager(page, service, state)

        # ──────────────────────────────────────────────────────────────
        # ESTABLECIMIENTO DE REFERENCIAS BIDIRECCIONALES
        # ──────────────────────────────────────────────────────────────
        # Conecta auth_manager con service y ui para permitir comunicación
        # bidireccional durante flujos de autenticación y actualización de UI.
        
        ui.auth_manager      = auth_manager
        service.auth_manager = auth_manager

        page.add(ui.root)

        async def _startup() -> None:
            """
            Rutina de inicialización post-renderizado.
            
            Ejecuta verificación de sesiones de autenticación y notifica
            al estado para actualizar la UI con el estado inicial de las
            plataformas conectadas.
            """
            await auth_manager.run_startup_check()
            state.notify()

        asyncio.create_task(_startup())

        async def _auth_poll_loop() -> None:
            """
            Bucle de sondeo periódico de sesiones de autenticación.
            
            Refresca los iconos de estado de autenticación cada 90 segundos
            para detectar expiraciones de tokens o cambios en las sesiones
            de las plataformas (Spotify, YouTube Music, Apple Music).
            
            Note:
                Intervalo de 90 segundos balanceado para detectar cambios
                sin generar tráfico excesivo a las APIs.
            """
            while True:
                await asyncio.sleep(90)
                await auth_manager.refresh_session_icons()

        ui.start_auth_poll(asyncio.create_task(_auth_poll_loop()))

        shutdown_event = asyncio.Event()
        _shutdown_once = [False]

        async def hard_cleanup(app_inst, state_inst, auth_inst, service_inst) -> None:
            """
            Protocolo de limpieza profunda de recursos del sistema.
            
            Implementa una estrategia de cierre ordenado que garantiza:
            1. Cancelación de circuit breakers (tareas de auto-reset)
            2. Detención de la instancia de UI (app_inst)
            3. Cancelación de tareas de escaneo lazy (state_inst)
            4. Cancelación de tareas de recarga de autenticación (auth_inst)
            5. Cancelación de todas las tareas asyncio pendientes
            6. Recolección de basura forzada (gc.collect)
            7. Limpieza de sesiones HTTP (service_inst)
            8. os._exit(0) como último recurso si threads bloqueados siguen vivos
            
            Args:
                app_inst: Instancia de PlaylistManagerUI.
                state_inst: Instancia de AppState.
                auth_inst: Instancia de AuthManager.
                service_inst: Instancia de MusicApiService.
            
            Note:
                Utiliza un flag _shutdown_once para prevenir ejecuciones
                múltiples y garantizar idempotencia. Este patrón es crítico
                para evitar condiciones de carrera durante el cierre y
                prevenir procesos huérfanos en el sistema operativo.
            """
            if _shutdown_once[0]:
                shutdown_event.set()
                return
            _shutdown_once[0] = True
            
            # Cancelar circuit breakers — evita tareas _auto_reset huérfanas
            # cuando se cierra durante un cooldown de rate-limit
            try:
                for cb in state_inst.cb.values():
                    cb.cancel()
            except Exception:  # pylint: disable=broad-exception-caught
                pass

            # Detener instancia de UI
            try:
                app_inst.stop()
            except Exception:  # pylint: disable=broad-exception-caught
                pass
            
            # Cancelar tareas de escaneo lazy
            try:
                state_inst.cancel_lazy_scan()
            except Exception:  # pylint: disable=broad-exception-caught
                pass
            
            # Cancelar tarea de recarga de autenticación si existe
            auth_reload = getattr(auth_inst, "_reload_task", None)
            if auth_reload and not auth_reload.done():
                auth_reload.cancel()
            
            # Cancelar todas las tareas asyncio pendientes excepto la actual
            current = asyncio.current_task()
            for task in asyncio.all_tasks():
                if task is current or task.done():
                    continue
                try:
                    name = task.get_name()
                    if "hard_cleanup" in name or "main" in name:
                        continue
                except AttributeError:
                    pass
                task.cancel()
            
            # Breve pausa para permitir cancelaciones pendientes
            await asyncio.sleep(0.05)
            
            # Forzar recolección de basura para liberar memoria
            import gc
            gc.collect()
            
            # Limpiar sesiones HTTP si el servicio lo soporta
            if hasattr(service_inst, "cleanup_sessions"):
                await asyncio.to_thread(service_inst.cleanup_sessions)
            
            shutdown_event.set()

            # Último recurso: si asyncio.to_thread tiene threads bloqueados
            # (p. ej. búsquedas en curso), no son cancelables — forzar salida.
            def _force_exit() -> None:
                import time as _time
                _time.sleep(3)
                os._exit(0)

            import threading as _threading
            _t = _threading.Thread(target=_force_exit, daemon=True, name="force-exit")
            _t.start()

        def _on_close(_) -> None:
            """
            Callback ejecutado al cerrar la ventana.
            
            Inicia el proceso de limpieza profunda de forma asíncrona
            para garantizar liberación ordenada de recursos.
            """
            asyncio.create_task(hard_cleanup(ui, state, auth_manager, service))

        page.on_close = _on_close
        await shutdown_event.wait()

    except asyncio.CancelledError:
        # Cierre normal de la aplicación, no requiere acción
        pass
    finally:
        # Último recurso: destruir ventana o forzar salida del proceso
        try:
            await page.window.destroy()
        except Exception:  # pylint: disable=broad-exception-caught
            os._exit(0)


if __name__ == "__main__":
    try:
        ft.run(main)
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Cierre por interrupción del usuario, salida limpia
        pass
