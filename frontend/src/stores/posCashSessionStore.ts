import { create } from "zustand";
import { persist } from "zustand/middleware";

/**
 * El turno abierto de cada cajero en cada caja de esta PC (B8).
 *
 * Hasta acá la caja creaba el turno con un `useRef` —uno NUEVO en cada montaje
 * de la pantalla—, así que cada recarga partía la jornada en otro turno con
 * fondo $0 que nadie cerraba. Ahora vive acá y sobrevive a una recarga.
 *
 * - **Clave propia de localStorage**, que `authStore.logout()` no toca, igual
 *   que `posTerminalStore`: cerrar la sesión no cierra el turno.
 * - **Uno por (negocio, usuario, caja)**, mismo criterio de pertenencia que la
 *   cola de B7a: si en esta PC entra otro cajero, abre SU turno y no hereda el
 *   del anterior, porque el arqueo queda firmado a nombre de quien lo cierra.
 * - **Se escribe ANTES de avisarle al servidor**: la apertura puede ser sin
 *   red, y el id del turno tiene que existir desde la primera venta.
 */

export interface TurnoLocal {
  /** El id que genera la caja. Viaja en cada venta. */
  clientSessionId: string;
  /** El id del servidor, cuando la apertura ya llegó. */
  serverId: string | null;
  /** El fondo: el declarado mientras no hay respuesta, el REAL después. */
  openingFloatCents: number;
  /** Lo que declaró quien abrió. Si difiere del real, se avisa. */
  declaredFloatCents: number;
  openedAt: string;
  /** `false` mientras la apertura no llegó al servidor (se abrió sin red). */
  aperturaConfirmada: boolean;
}

export function claveDeTurno(
  tenantId: string,
  userId: string,
  terminalId: string | null,
): string {
  return `${tenantId}:${userId}:${terminalId ?? "sin-terminal"}`;
}

interface PosCashSessionState {
  turnos: Record<string, TurnoLocal>;
  guardar: (clave: string, turno: TurnoLocal) => void;
  actualizar: (clave: string, cambios: Partial<TurnoLocal>) => void;
  quitar: (clave: string) => void;
  /**
   * Si esta PC tiene UN turno de este usuario bajo otra caja, lo pasa a
   * `clave`. Devuelve si movió algo. Ver `turnoDeOtraCaja`.
   */
  adoptarDeOtraCaja: (tenantId: string, userId: string, clave: string) => boolean;
}

/**
 * El turno que quedó bajo otra clave de caja en ESTA PC.
 *
 * La clave incluye la caja, y la caja cambia sin que cambie el cajón: el dueño
 * que venía cobrando desde el navegador habilita la PC como caja, o una caja
 * dada de baja se vuelve a habilitar con otro id. Sin esto, el turno abierto
 * quedaba huérfano en esta PC y abierto en el servidor, y la caja pedía abrir
 * otro. Como `localStorage` es de la PC, un turno guardado acá de este mismo
 * usuario es el de este cajón físico.
 *
 * Sólo si hay UNO: con más de uno no se sabe cuál es el vigente, y elegir mal
 * mezclaría dos arqueos. Esos se cierran desde el panel del encargado.
 */
export function turnoDeOtraCaja(
  turnos: Record<string, TurnoLocal>,
  tenantId: string,
  userId: string,
  clave: string,
): string | null {
  if (turnos[clave]) return null;
  const prefijo = `${tenantId}:${userId}:`;
  const otras = Object.keys(turnos).filter((k) => k !== clave && k.startsWith(prefijo));
  return otras.length === 1 ? otras[0]! : null;
}

export const usePosCashSessionStore = create<PosCashSessionState>()(
  persist(
    (set, get) => ({
      turnos: {},
      guardar: (clave, turno) => set((s) => ({ turnos: { ...s.turnos, [clave]: turno } })),
      actualizar: (clave, cambios) =>
        set((s) => {
          const actual = s.turnos[clave];
          if (!actual) return s;
          return { turnos: { ...s.turnos, [clave]: { ...actual, ...cambios } } };
        }),
      quitar: (clave) =>
        set((s) => {
          const { [clave]: _quitado, ...resto } = s.turnos;
          return { turnos: resto };
        }),
      adoptarDeOtraCaja: (tenantId, userId, clave) => {
        const origen = turnoDeOtraCaja(get().turnos, tenantId, userId, clave);
        if (!origen) return false;
        set((s) => {
          const { [origen]: turno, ...resto } = s.turnos;
          return turno ? { turnos: { ...resto, [clave]: turno } } : s;
        });
        return true;
      },
    }),
    { name: "vektor-pos-turno" },
  ),
);
