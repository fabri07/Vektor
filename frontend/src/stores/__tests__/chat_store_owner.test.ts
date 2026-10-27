import { useAuthStore } from "@/stores/authStore";
import { useChatStore } from "@/stores/chatStore";

// X6-2: la conversación guardada en `vektor_chat` es de un usuario. Antes no
// tenía dueño y ni el logout ni un login nuevo la reseteaban: el siguiente
// usuario de la PC veía la conversación anterior y seguía mandando su id.

const usuario = (id: string, tenantId: string) => ({
  id,
  email: `${id}@test.com`,
  full_name: id,
  role: "OWNER",
  tenant_id: tenantId,
});

function conversar(): string {
  useChatStore.getState().addMessage({ role: "user", content: "el margen de A" });
  return useChatStore.getState().conversationId;
}

function guardado(): string | null {
  return window.localStorage.getItem("vektor_chat");
}

describe("chatStore — dueño de la conversación", () => {
  beforeEach(() => {
    window.localStorage.clear();
    useChatStore.setState({ conversationId: "inicial", messages: [], ownerKey: null });
  });

  it("otro usuario que entra en la misma PC arranca vacío y con otro id", () => {
    useAuthStore.getState().setAuth("t", "r", usuario("u-a", "t-a"));
    const deA = conversar();

    useAuthStore.getState().setAuth("t2", "r2", usuario("u-b", "t-b"));

    const estado = useChatStore.getState();
    expect(estado.messages).toEqual([]);
    expect(estado.conversationId).not.toBe(deA);
    expect(guardado()).not.toContain("el margen de A");
  });

  it("otro usuario del MISMO negocio tampoco hereda la conversación", () => {
    useAuthStore.getState().setAuth("t", "r", usuario("u-a", "t-a"));
    const deA = conversar();

    useAuthStore.getState().setAuth("t2", "r2", usuario("u-c", "t-a"));

    expect(useChatStore.getState().messages).toEqual([]);
    expect(useChatStore.getState().conversationId).not.toBe(deA);
  });

  it("el mismo usuario que vuelve a loguearse sigue su conversación", () => {
    useAuthStore.getState().setAuth("t", "r", usuario("u-a", "t-a"));
    const deA = conversar();

    useAuthStore.getState().setAuth("t2", "r2", usuario("u-a", "t-a"));

    expect(useChatStore.getState().conversationId).toBe(deA);
    expect(useChatStore.getState().messages).toHaveLength(1);
  });

  it("el logout descarta la conversación y la borra del almacenamiento", () => {
    useAuthStore.getState().setAuth("t", "r", usuario("u-a", "t-a"));
    const deA = conversar();
    expect(guardado()).toContain("el margen de A");

    try {
      useAuthStore.getState().logout();
    } catch {
      // jsdom puede quejarse de la navegación a /login; no importa acá.
    }

    expect(useChatStore.getState().messages).toEqual([]);
    expect(useChatStore.getState().conversationId).not.toBe(deA);
    expect(guardado()).toBeNull();
  });

  it("al recargar la página con la sesión de otro usuario, la conversación se descarta", async () => {
    window.localStorage.setItem(
      "vektor_chat",
      JSON.stringify({
        state: {
          conversationId: "conv-de-a",
          messages: [{ id: "m1", role: "user", content: "el margen de A", timestamp: "x" }],
          ownerKey: "t-a:u-a",
        },
        version: 0,
      }),
    );
    window.localStorage.setItem(
      "vektor_auth",
      JSON.stringify({
        state: { token: "t", refreshToken: "r", user: usuario("u-b", "t-b") },
        version: 0,
      }),
    );

    await useChatStore.persist.rehydrate();
    expect(useChatStore.getState().conversationId).toBe("conv-de-a");
    await useAuthStore.persist.rehydrate();

    expect(useChatStore.getState().messages).toEqual([]);
    expect(useChatStore.getState().conversationId).not.toBe("conv-de-a");
    expect(guardado()).not.toContain("el margen de A");
  });

  it("una conversación guardada sin dueño (anterior al arreglo) se descarta", () => {
    conversar();

    useChatStore.getState().bindOwner({ userId: "u-a", tenantId: "t-a" });

    expect(useChatStore.getState().messages).toEqual([]);
    expect(useChatStore.getState().ownerKey).toBe("t-a:u-a");
  });
});
