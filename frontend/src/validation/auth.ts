import { z } from "zod";

export const loginSchema = z.object({
  email: z.string().email("Email inválido"),
  password: z.string().min(8, "Mínimo 8 caracteres"),
});

export const registerSchema = z.object({
  email: z.string().email("Email inválido"),
  password: z.string().min(8, "Mínimo 8 caracteres"),
  full_name: z.string().min(2, "Nombre requerido"),
  business_name: z.string().min(2, "Nombre del negocio requerido"),
  vertical_code: z.enum(["kiosco", "decoracion_hogar", "limpieza"], {
    errorMap: () => ({ message: "Seleccioná un rubro" }),
  }),
});

export type LoginInput = z.infer<typeof loginSchema>;
export type RegisterInput = z.infer<typeof registerSchema>;
