import { configureStore } from "@reduxjs/toolkit";
import { setSessionExpiredHandler } from "../api/client";
import assistant from "./assistantSlice";
import auth, { sessionExpired } from "./authSlice";
import toasts from "./toastSlice";
import uploads from "./uploadsSlice";

export const store = configureStore({
  reducer: { auth, toasts, assistant, uploads },
});

// The API client reports an unrecoverable 401 (refresh failed) to the store.
setSessionExpiredHandler(() => store.dispatch(sessionExpired()));

export type RootState = ReturnType<typeof store.getState>;
export type AppDispatch = typeof store.dispatch;
