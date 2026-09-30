import { createSlice, type PayloadAction } from "@reduxjs/toolkit";

export type ToastTone = "ok" | "bad" | "info";
export interface Toast {
  id: number;
  tone: ToastTone;
  message: string;
}

let nextId = 1;

const toastSlice = createSlice({
  name: "toasts",
  initialState: [] as Toast[],
  reducers: {
    pushToast: {
      reducer(state, action: PayloadAction<Toast>) {
        state.push(action.payload);
      },
      prepare(tone: ToastTone, message: string) {
        return { payload: { id: nextId++, tone, message } };
      },
    },
    dismissToast(state, action: PayloadAction<number>) {
      return state.filter((toast) => toast.id !== action.payload);
    },
  },
});

export const { pushToast, dismissToast } = toastSlice.actions;
export default toastSlice.reducer;
