import { createAsyncThunk, createSlice, type PayloadAction } from "@reduxjs/toolkit";
import { api, ApiError } from "../api/client";
import type { Answer, ConversationDetail, Source } from "../api/types";

export type AnswerMode = "auto" | "current" | "historical" | "version" | "compare";

export interface AskOptions {
  mode: AnswerMode;
  as_of?: string;
  version_ids?: string[];
  policy_ids?: string[];
  category_ids?: string[];
}

export type AnswerStage = "searching" | "reading" | "writing";

/** A claim that has already passed validation, shown while the answer is still being written. */
export interface StreamedClaim {
  text: string;
  citations: number[];
}

export interface Turn {
  id: string;
  question: string;
  options: AskOptions;
  status: "pending" | "done" | "error";
  answer?: Answer;
  error?: string;
  stage?: AnswerStage;
  passages?: number;
  streamedClaims?: StreamedClaim[];
  streamedSources?: Source[];
}

interface StreamEvent {
  id: string;
  event: string;
  data: any;
}

interface AssistantState {
  turns: Turn[];
  options: AskOptions;
  /** The saved conversation being continued; null for a new chat (the first answer creates it). */
  conversationId: string | null;
  /** A saved conversation being opened. */
  loading: string | null;
}

const initialState: AssistantState = { turns: [], options: { mode: "auto" }, conversationId: null, loading: null };

/** Reopen a saved conversation: its turns come back exactly as they were answered. */
export const openConversation = createAsyncThunk(
  "assistant/openConversation",
  (id: string) => api.get<ConversationDetail>(`/ai/conversations/${id}`),
);

// Earlier turns sent with a question so follow-ups ("what about that
// scheme?") can be resolved. The server uses them only to restate the
// question, never as evidence.
const HISTORY_TURNS = 4;

export const ask = createAsyncThunk(
  "assistant/ask",
  async ({ id, question, options }: { id: string; question: string; options: AskOptions }, { getState, dispatch }) => {
    const { assistant } = getState() as { assistant: AssistantState };
    const history = assistant.turns
      .filter((turn) => turn.id !== id && turn.status === "done")
      .slice(-HISTORY_TURNS)
      .map((turn) => ({ question: turn.question, answer: turn.answer?.answer ?? null }));
    const body = { question, history, conversation_id: assistant.conversationId, ...options };
    // Streamed: progress and each validated claim appear as soon as they exist;
    // the final "done" event is the same answer /ai/ask would return.
    let answer: Answer | null = null;
    let failure: { code?: string; message?: string } | null = null;
    try {
      await api.stream("/ai/ask/stream", body, (event, data) => {
        if (event === "done") answer = data as Answer;
        else if (event === "error") failure = data as { code?: string; message?: string };
        else dispatch(assistantSlice.actions.streamEvent({ id, event, data }));
      });
    } catch (error) {
      // A backend without the streaming route: fall back to the plain request.
      if (error instanceof ApiError && (error.status === 404 || error.status === 405)) {
        return api.post<Answer>("/ai/ask", body);
      }
      throw error;
    }
    if (failure) throw new ApiError(500, (failure as { code?: string }).code ?? "ANSWER_FAILED", (failure as { message?: string }).message ?? "The answer could not be completed.");
    if (!answer) throw new ApiError(502, "STREAM_INTERRUPTED", "The connection closed before the answer was complete. Please try again.");
    return answer as Answer;
  },
);

const assistantSlice = createSlice({
  name: "assistant",
  initialState,
  reducers: {
    setOptions(state, action: PayloadAction<AskOptions>) {
      state.options = action.payload;
    },
    clearConversation(state) {
      state.turns = [];
      state.conversationId = null;
    },
    streamEvent(state, action: PayloadAction<StreamEvent>) {
      const turn = state.turns.find((t) => t.id === action.payload.id);
      if (!turn || turn.status !== "pending") return;
      const { event, data } = action.payload;
      if (event === "stage") {
        turn.stage = data.stage;
        if (typeof data.passages === "number") turn.passages = data.passages;
      } else if (event === "claim") {
        turn.stage = "writing";
        turn.streamedClaims = [...(turn.streamedClaims ?? []), { text: data.text, citations: data.citations }];
        turn.streamedSources = [...(turn.streamedSources ?? []), ...(data.sources ?? [])];
      }
    },
  },
  extraReducers: (builder) => {
    builder
      .addCase(ask.pending, (state, action) => {
        const { id, question, options } = action.meta.arg;
        state.turns.push({ id, question, options, status: "pending" });
      })
      .addCase(ask.fulfilled, (state, action) => {
        const turn = state.turns.find((t) => t.id === action.meta.arg.id);
        if (turn) Object.assign(turn, { status: "done", answer: action.payload, streamedClaims: undefined, streamedSources: undefined });
        // Only adopt the id while this chat is still on screen (not after "New chat" mid-answer).
        if (turn && action.payload.conversation_id) state.conversationId = action.payload.conversation_id;
      })
      .addCase(openConversation.pending, (state, action) => {
        state.loading = action.meta.arg;
      })
      .addCase(openConversation.fulfilled, (state, action) => {
        if (state.loading !== action.meta.arg) return;  // another chat was opened meanwhile
        state.loading = null;
        state.conversationId = action.payload.id;
        state.turns = action.payload.messages.map((message) => ({
          id: message.id,
          question: message.question,
          options: { mode: "auto", ...(message.options as Partial<AskOptions>) },
          status: "done",
          answer: message.answer,
        }));
      })
      .addCase(openConversation.rejected, (state, action) => {
        if (state.loading === action.meta.arg) state.loading = null;
      })
      .addCase(ask.rejected, (state, action) => {
        const turn = state.turns.find((t) => t.id === action.meta.arg.id);
        const error = action.error as ApiError;
        if (turn) Object.assign(turn, { status: "error", error: error.message ?? "The request failed." });
      })
      // A signed-out session must never leave its conversation for the next user.
      .addMatcher(
        (action) => action.type === "auth/logout/fulfilled" || action.type === "auth/sessionExpired",
        () => initialState,
      );
  },
});

export const { setOptions, clearConversation } = assistantSlice.actions;
export default assistantSlice.reducer;
