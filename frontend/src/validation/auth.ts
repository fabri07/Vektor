import { z } from "zod";
import { validateOptionalPhone } from "@/lib/fiscal";

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
  phone: z
    .string()
    .optional()
    .superRefine((v, ctx) => {
      const error = validateOptionalPhone(v);
      if (error) ctx.addIssue({ code: z.ZodIssueCode.custom, message: error });
    }),
});

export type LoginInput = z.infer<typeof loginSchema>;
export type RegisterInput = z.infer<typeof registerSchema>;
