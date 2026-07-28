/**
 * Mutaciones del trámite de solicitud de acceso.
 *
 * Espeja `useRegister` (`hooks/useAuth.ts`) pero **sin `setAuth`**: acá no hay
 * tokens que guardar porque no se creó ninguna cuenta — la solicitud queda
 * esperando la revisión manual del dueño.
 */

import { useMutation } from "@tanstack/react-query";

import {
  createAccessRequest,
  verifyAccessRequest,
  type AccessRequestPayload,
} from "@/services/accessRequest.service";

export function useCreateAccessRequest() {
  return useMutation({
    mutationFn: (payload: AccessRequestPayload) => createAccessRequest(payload),
  });
}

export function useVerifyAccessRequest() {
  return useMutation({
    mutationFn: (token: string) => verifyAccessRequest(token),
  });
}
