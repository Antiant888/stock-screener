import { render, renderHook } from '@testing-library/react';
import { ThemeProvider, createTheme } from '@mui/material';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { getDesignTokens } from '../theme';

const darkTheme = createTheme(getDesignTokens('dark'));

function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });
}

function createWrapper(queryClient) {
  return function Wrapper({ children }) {
    return (
      <QueryClientProvider client={queryClient}>
        <ThemeProvider theme={darkTheme}>{children}</ThemeProvider>
      </QueryClientProvider>
    );
  };
}

export function renderWithProviders(ui, { queryClient = createTestQueryClient(), ...options } = {}) {
  const Wrapper = createWrapper(queryClient);

  return {
    queryClient,
    ...render(ui, { wrapper: Wrapper, ...options }),
  };
}

export function renderHookWithProviders(callback, options = {}) {
  const queryClient = createTestQueryClient();
  const Wrapper = createWrapper(queryClient);

  return {
    queryClient,
    ...renderHook(callback, { wrapper: Wrapper, ...options }),
  };
}
