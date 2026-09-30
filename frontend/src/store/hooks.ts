import { useCallback } from "react";
import { useDispatch, useSelector } from "react-redux";
import type { AppDispatch, RootState } from ".";
import { login, logout } from "./authSlice";
import { dismissToast, pushToast, type ToastTone } from "./toastSlice";

export const useAppDispatch = useDispatch.withTypes<AppDispatch>();
export const useAppSelector = useSelector.withTypes<RootState>();

const TOAST_MS = 5000;

/** Convenience hook over the auth slice. */
export function useAuth() {
  const dispatch = useAppDispatch();
  const { me, status } = useAppSelector((state) => state.auth);
  const can = useCallback(
    (...permissions: string[]) => Boolean(me) && permissions.every((p) => me!.permissions.includes(p)),
    [me],
  );
  return {
    me,
    loading: status === "restoring",
    can,
    login: (email: string, password: string) => dispatch(login({ email, password })).unwrap(),
    logout: () => dispatch(logout()).unwrap(),
  };
}

export function useToast() {
  const dispatch = useAppDispatch();
  return useCallback(
    (tone: ToastTone, message: string) => {
      const { payload } = dispatch(pushToast(tone, message));
      setTimeout(() => dispatch(dismissToast(payload.id)), TOAST_MS);
    },
    [dispatch],
  );
}
