import { z } from "zod";

export const loginSchema = z.object({
  email: z.string().email("Email inválido"),
  password: z.string().min(8, "Mínimo 8 caracteres"),
});

export type LoginInput = z.infer<typeof loginSchema>;

// `registerSchema` se eliminó junto con el registro abierto: el alta pública ya
// no crea una cuenta, manda una solicitud. Su reemplazo es
// `validation/accessRequest.ts` — que, entre otras cosas, NO tiene campo
// `password`.
