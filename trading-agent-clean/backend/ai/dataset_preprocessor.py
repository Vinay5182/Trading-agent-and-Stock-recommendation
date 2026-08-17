import os
import joblib
import pandas as pd
import numpy as np
from typing import List, Tuple
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler, OneHotEncoder


class DatasetPreprocessor:
    """
    Production-quality feature preprocessor for tabular trading datasets.
    Handles numeric scaling, categorical one-hot encoding, and missing value imputation.
    Ensures identical feature ordering across training, validation, and inference.
    """

    def __init__(self):
        self.numeric_cols: List[str] = []
        self.categorical_cols: List[str] = []
        self.feature_names_out: List[str] = []

        self.num_imputer = SimpleImputer(strategy="median")
        self.scaler = StandardScaler()
        self.cat_encoder = OneHotEncoder(handle_unknown="ignore", sparse_output=False)
        self.is_fitted: bool = False

    def _infer_column_types(self, X: pd.DataFrame):
        self.numeric_cols = [
            c for c in X.columns if pd.api.types.is_numeric_dtype(X[c]) and not pd.api.types.is_bool_dtype(X[c])
        ]
        self.categorical_cols = [
            c for c in X.columns if c not in self.numeric_cols
        ]

    def fit(self, X: pd.DataFrame):
        self._infer_column_types(X)

        if self.numeric_cols:
            X_num = X[self.numeric_cols].values
            X_num_imp = self.num_imputer.fit_transform(X_num)
            self.scaler.fit(X_num_imp)

        if self.categorical_cols:
            X_cat = X[self.categorical_cols].astype(str).values
            self.cat_encoder.fit(X_cat)
            cat_feature_names = list(self.cat_encoder.get_feature_names_out(self.categorical_cols))
        else:
            cat_feature_names = []

        self.feature_names_out = self.numeric_cols + cat_feature_names
        self.is_fitted = True
        return self

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        if not self.is_fitted:
            raise RuntimeError("DatasetPreprocessor must be fitted before calling transform().")

        processed_parts = []

        if self.numeric_cols:
            X_num = X[self.numeric_cols].values
            X_num_imp = self.num_imputer.transform(X_num)
            X_num_scaled = self.scaler.transform(X_num_imp)
            processed_parts.append(pd.DataFrame(X_num_scaled, columns=self.numeric_cols, index=X.index))

        if self.categorical_cols:
            X_cat = X[self.categorical_cols].astype(str).values
            X_cat_enc = self.cat_encoder.transform(X_cat)
            cat_feature_names = list(self.cat_encoder.get_feature_names_out(self.categorical_cols))
            processed_parts.append(pd.DataFrame(X_cat_enc, columns=cat_feature_names, index=X.index))

        if processed_parts:
            df_out = pd.concat(processed_parts, axis=1)
        else:
            df_out = pd.DataFrame(index=X.index)

        # Guarantee column ordering matching feature_names_out
        for col in self.feature_names_out:
            if col not in df_out.columns:
                df_out[col] = 0.0

        return df_out[self.feature_names_out]

    def fit_transform(self, X: pd.DataFrame) -> pd.DataFrame:
        return self.fit(X).transform(X)

    def save(self, file_path: str):
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        joblib.dump(self, file_path)

    @classmethod
    def load(cls, file_path: str) -> "DatasetPreprocessor":
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Preprocessor file not found at: {file_path}")
        return joblib.load(file_path)
