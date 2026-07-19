"use client";

import {
    createContext,
    useCallback,
    useContext,
    useMemo,
    useRef,
    useState,
    type ReactNode,
} from "react";
import { createChat, type Chat } from "@/lib/chatStore";

/**
 * React wiring over the pure `chatStore` logic — the adapted, backend-free
 * `ChatHistoryContext`. All state is session-scoped in memory and lost on
 * refresh (no persistence, no auto-title). The pure store functions carry the
 * windowing/leakage rules; this provider only holds the chat list, the active
 * id, and per-chat mutators the chat hook uses.
 */
interface ChatStoreContextValue {
    chats: Chat[];
    activeChatId: string;
    activeChat: Chat;
    newChat: () => void;
    selectChat: (id: string) => void;
    /** Mutate a specific chat by id (a streaming turn keeps updating its own
     *  chat even if the user switches away mid-stream). */
    updateChat: (id: string, updater: (chat: Chat) => Chat) => void;
    updateActiveChat: (updater: (chat: Chat) => Chat) => void;
}

const ChatStoreContext = createContext<ChatStoreContextValue | undefined>(
    undefined,
);

export function ChatStoreProvider({ children }: { children: ReactNode }) {
    const seqRef = useRef(1);
    const [chats, setChats] = useState<Chat[]>(() => [createChat(1)]);
    const [activeChatId, setActiveChatId] = useState<string>(() => chats[0].id);

    const newChat = useCallback(() => {
        seqRef.current += 1;
        const chat = createChat(seqRef.current);
        setChats((prev) => [chat, ...prev]);
        setActiveChatId(chat.id);
    }, []);

    const selectChat = useCallback((id: string) => {
        setActiveChatId(id);
    }, []);

    const updateChat = useCallback(
        (id: string, updater: (chat: Chat) => Chat) => {
            setChats((prev) =>
                prev.map((chat) => (chat.id === id ? updater(chat) : chat)),
            );
        },
        [],
    );

    const updateActiveChat = useCallback(
        (updater: (chat: Chat) => Chat) => updateChat(activeChatId, updater),
        [updateChat, activeChatId],
    );

    const activeChat = useMemo(
        () => chats.find((chat) => chat.id === activeChatId) ?? chats[0],
        [chats, activeChatId],
    );

    const value = useMemo(
        () => ({
            chats,
            activeChatId,
            activeChat,
            newChat,
            selectChat,
            updateChat,
            updateActiveChat,
        }),
        [
            chats,
            activeChatId,
            activeChat,
            newChat,
            selectChat,
            updateChat,
            updateActiveChat,
        ],
    );

    return (
        <ChatStoreContext.Provider value={value}>
            {children}
        </ChatStoreContext.Provider>
    );
}

export function useChatStore(): ChatStoreContextValue {
    const context = useContext(ChatStoreContext);
    if (!context) {
        throw new Error("useChatStore must be used within a ChatStoreProvider");
    }
    return context;
}
