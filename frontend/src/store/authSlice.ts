import { createAsyncThunk, createSlice } from "@reduxjs/toolkit";
import { api, getRefreshToken, hasRefreshToken, setTokens } from "../api/client";
import type { Me, TokenPair } from "../api/types";
import { queryClient } from "../lib/queryClient";

type Status = "restoring" | "authenticated" | "anonymous";

interface AuthState {
  me: Me | null;
  status: Status;
}

const initialState: AuthState = { me: null, status: hasRefreshToken() ? "restoring" : "anonymous" };

/** Restore a session after reload using the refresh token (kept in sessionStorage). */
export const restoreSession = createAsyncThunk("auth/restore", async () => {
  const tokens = await api.post<TokenPair>("/auth/refresh", { refresh_token: getRefreshToken() });
  setTokens(tokens.access_token, tokens.refresh_token);
  return api.get<Me>("/auth/me");
});

export const login = createAsyncThunk("auth/login", async ({ email, password }: { email: string; password: string }) => {
  const tokens = await api.post<TokenPair>("/auth/login", { email, password });
  setTokens(tokens.access_token, tokens.refresh_token);
  return api.get<Me>("/auth/me");
});

export const logout = createAsyncThunk("auth/logout", async () => {
  const refresh = getRefreshToken();
  if (refresh) await api.post("/auth/logout", { refresh_token: refresh }).catch(() => undefined);
  setTokens(null, null);
  queryClient.clear();
});

const authSlice = createSlice({
  name: "auth",
  initialState,
  reducers: {
    sessionExpired(state) {
      setTokens(null, null);
      queryClient.clear();
      state.me = null;
      state.status = "anonymous";
    },
  },
  extraReducers: (builder) => {
    builder
      .addCase(restoreSession.fulfilled, (state, action) => {
        state.me = action.payload;
        state.status = "authenticated";
      })
      .addCase(restoreSession.rejected, (state) => {
        setTokens(null, null);
        state.me = null;
        state.status = "anonymous";
      })
      .addCase(login.fulfilled, (state, action) => {
        state.me = action.payload;
        state.status = "authenticated";
      })
      .addCase(logout.fulfilled, (state) => {
        state.me = null;
        state.status = "anonymous";
      });
  },
});

export const { sessionExpired } = authSlice.actions;
export default authSlice.reducer;
